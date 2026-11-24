"""
Inference layer.

Two responsibilities, kept together because they are the same decision:

1. **Route a task to a model.** Deciding *which* model handles a task is an
   engineering choice, so it lives in one explicit table (:data:`TASK_ROUTING`)
   rather than being scattered across call sites.
2. **Survive a flaky provider.** A ``503`` from a hosted model is a queue, not a
   bug. Every call is retried on transient failures with linear backoff, then
   tried against a fallback model, and only then degraded to a deterministic rule
   engine — which is also what runs when no provider is configured at all.

A degradation is never silent: the returned :class:`ModelCallResult` records the
model actually used, the strategy, the number of attempts and which model (if any)
it degraded from. The API key is sent in a header, never in a URL, and is scrubbed
from any string that could reach a log or the database.
"""
import json
import time
from dataclasses import dataclass, field

import requests
from django.conf import settings

RULES_MODEL = 'rule-engine-v1'

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

TIER_FAST = 'fast'
TIER_REASONING = 'reasoning'
TIER_RULES = 'rules'


@dataclass
class ModelCallResult:
    output: dict
    model: str
    strategy: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: float = 0.0
    latency_ms: int = 0
    fallback: bool = False
    error: str = ''
    meta: dict = field(default_factory=dict)

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens


TASK_ROUTING = {
    'CUSTOMER_ANALYSIS': {
        'tier': TIER_REASONING,
        'why': (
            'Low volume, high stakes: decides whether to contact a real person and '
            'how large an incentive to propose. Worth the stronger model.'
        ),
    },
    'MESSAGE_DRAFTING': {
        'tier': TIER_FAST,
        'why': (
            'High volume, low stakes, and bounded by the analysis — the message can '
            'only restate a decision already made. Cheap model is enough.'
        ),
    },
    'REPLY_CLASSIFICATION': {
        'tier': TIER_FAST,
        'why': 'Short text, three labels, high volume.',
    },
    'ELIGIBILITY': {
        'tier': TIER_RULES,
        'why': (
            'Whether a customer may be contacted must be reproducible and auditable, '
            'and it decides who gets messaged. Never a model.'
        ),
    },
    'DISCOUNT_POLICY': {
        'tier': TIER_RULES,
        'why': (
            'Whether an incentive needs the owner must not be negotiable by the '
            'component that wants the incentive. Never a model.'
        ),
    },
}


def describe_routing():
    """Expose the routing table, resolved models and all, for inspection."""
    resolved = {}
    for task, spec in TASK_ROUTING.items():
        tier = spec['tier']
        if tier == TIER_REASONING:
            model = settings.LLM_REASONING_MODEL
        elif tier == TIER_FAST:
            model = settings.LLM_FAST_MODEL
        else:
            model = RULES_MODEL
        resolved[task] = {
            'tier': tier,
            'model': model,
            'why': spec['why'],
            'candidate_chain': candidate_models(tier),
        }
    return {
        'provider': settings.LLM_PROVIDER or 'none (deterministic rule engine)',
        'live': bool(settings.LLM_PROVIDER and settings.LLM_API_KEY),
        'tasks': resolved,
        'resilience': {
            'max_attempts': settings.LLM_MAX_ATTEMPTS,
            'retry_backoff_seconds': settings.LLM_RETRY_BACKOFF_SECONDS,
            'timeout_seconds': settings.LLM_TIMEOUT_SECONDS,
            'fallback_models': settings.LLM_FALLBACK_MODELS,
            'retryable_statuses': sorted(RETRYABLE_STATUS),
        },
    }


def candidate_models(tier):
    """The ordered chain for a tier: primary first, then the configured fallbacks."""
    if tier == TIER_RULES:
        return [RULES_MODEL]
    primary = settings.LLM_REASONING_MODEL if tier == TIER_REASONING else settings.LLM_FAST_MODEL
    chain = [primary]
    for model in settings.LLM_FALLBACK_MODELS:
        if model and model not in chain:
            chain.append(model)
    return chain


def estimate_cost(model, prompt_tokens, completion_tokens):
    rates = settings.LLM_PRICING.get(model)
    if not rates:
        return 0.0
    return round(
        (prompt_tokens / 1_000_000) * rates['in'] + (completion_tokens / 1_000_000) * rates['out'],
        8,
    )


def redact(text):
    """Remove the configured API key from anything we are about to store.

    Defence in depth: the key travels in a header so it should never appear in an
    exception message, but a leaked key in a database row is bad enough that it is
    worth scrubbing twice.
    """
    text = str(text)
    key = settings.LLM_API_KEY
    if key:
        text = text.replace(key, '[redacted]')
    return text


def run_task(task, payload):
    """Run one inference task and always return a usable result.

    Never raises. On any failure the deterministic rule engine produces a valid
    answer for that task, and the degradation is recorded.
    """
    tier = TASK_ROUTING[task]['tier']

    if tier == TIER_RULES or not (settings.LLM_PROVIDER and settings.LLM_API_KEY):
        return _deterministic_result(task, payload)

    prompt = build_prompt(task, payload)
    chain = candidate_models(tier)
    failures = []

    for index, model in enumerate(chain):
        try:
            started = time.perf_counter()
            text, prompt_tokens, completion_tokens, attempts = _invoke_with_retry(model, prompt)
            parsed = _parse_json(text)
            if not isinstance(parsed, dict):
                raise ValueError('Model response was not a JSON object.')
            return ModelCallResult(
                output=parsed,
                model=model,
                strategy='llm' if index == 0 else 'llm-model-fallback',
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost=estimate_cost(model, prompt_tokens, completion_tokens),
                latency_ms=int((time.perf_counter() - started) * 1000),
                fallback=index > 0,
                meta={
                    'attempts': attempts,
                    'chain': chain,
                    'degraded_from': chain[0] if index else '',
                },
            )
        except Exception as exc:  # noqa: BLE001 — any failure must degrade, not crash
            failures.append(redact(f'{model}: {type(exc).__name__}: {exc}')[:160])

    result = _deterministic_result(task, payload)
    result.error = ' | '.join(failures)[:300]
    result.meta['failures'] = failures
    result.meta['chain'] = chain
    return result


def _deterministic_result(task, payload):
    start = time.perf_counter()
    return ModelCallResult(
        output=deterministic(task, payload),
        model=RULES_MODEL,
        strategy='deterministic-fallback' if _provider_configured() else 'deterministic',
        latency_ms=int((time.perf_counter() - start) * 1000),
        meta={'chain': [RULES_MODEL]},
    )


def _provider_configured():
    return bool(settings.LLM_PROVIDER and settings.LLM_API_KEY)


def _invoke_with_retry(model, prompt):
    """Call one model, retrying only transient failures.

    Retrying a ``400`` or a ``404`` just burns the latency budget, so those fail
    fast and let the caller move to the next model.
    """
    attempts_allowed = max(1, int(settings.LLM_MAX_ATTEMPTS or 1))
    backoff = float(settings.LLM_RETRY_BACKOFF_SECONDS or 0)
    last_error = None

    for attempt in range(1, attempts_allowed + 1):
        try:
            text, prompt_tokens, completion_tokens = _invoke(model, prompt)
            return text, prompt_tokens, completion_tokens, attempt
        except requests.HTTPError as exc:
            status = getattr(exc.response, 'status_code', None)
            if status not in RETRYABLE_STATUS:
                raise
            last_error = exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc

        if attempt < attempts_allowed and backoff:
            time.sleep(backoff * attempt)

    raise last_error


def _invoke(model, prompt):
    """One provider call. Returns ``(text, prompt_tokens, completion_tokens)``."""
    if settings.LLM_PROVIDER != 'gemini':
        raise ValueError(f'Unsupported LLM_PROVIDER {settings.LLM_PROVIDER!r}.')

    url = f'{settings.LLM_BASE_URL}/models/{model}:generateContent'
    payload = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 1200},
    }
    response = requests.post(
        url,
        json=payload,
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': settings.LLM_API_KEY,
        },
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()

    candidates = body.get('candidates') or []
    if not candidates:
        raise ValueError('Provider returned no candidates.')

    text = _text_from_parts(candidates[0].get('content', {}).get('parts', []))
    if not text.strip():
        raise ValueError('Provider returned an empty response.')

    usage = body.get('usageMetadata') or {}
    return text, int(usage.get('promptTokenCount') or 0), int(usage.get('candidatesTokenCount') or 0)


def _text_from_parts(parts):
    """Join the answer parts, skipping the model's private reasoning parts.

    Thinking models emit ``thought: true`` parts that are not the answer.
    """
    chunks = []
    for part in parts:
        if part.get('thought'):
            continue
        if 'text' in part:
            chunks.append(part['text'])
    return '\n'.join(chunks)


def _parse_json(text):
    """Extract the first JSON object from a model response.

    Models wrap JSON in prose or fences often enough that a bare ``json.loads``
    is not enough. We scan for the first balanced object rather than trusting the
    whole string.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


_PROMPTS = {
    'CUSTOMER_ANALYSIS': (
        'You are the analysis step of a customer win-back worker for a small Indian '
        'online retailer. Decide whether one follow-up message is warranted and what '
        'incentive (if any) to propose.\n\n'
        'Be conservative: propose a discount only when the history justifies it, and '
        'never propose more than the stated cap.\n\n'
        'Return ONLY a JSON object with these keys:\n'
        '  "should_follow_up": boolean,\n'
        '  "reason": short string explaining the decision,\n'
        '  "discount_pct": integer between 0 and the cap,\n'
        '  "urgency": one of "low", "normal", "high",\n'
        '  "tone": a short description of the right tone.\n\n'
        'Customer context:\n{context}'
    ),
    'MESSAGE_DRAFTING': (
        'Write one short, friendly win-back message for a small Indian online retailer. '
        'It must be under 300 characters, personal, and contain no invented facts — use '
        'only the details provided. Write it in the requested language.\n\n'
        'Return ONLY a JSON object with the key "message".\n\n'
        'Brief:\n{brief}'
    ),
    'REPLY_CLASSIFICATION': (
        'Classify a customer reply to a win-back message.\n\n'
        'Return ONLY a JSON object with these keys:\n'
        '  "intent": one of "POSITIVE", "NEGATIVE", "QUESTION",\n'
        '  "summary": a short factual summary of what the customer said,\n'
        '  "wants_no_contact": boolean.\n\n'
        'Reply:\n{reply}'
    ),
}


def build_prompt(task, payload):
    return _PROMPTS[task].format(**{key: _render(value) for key, value in payload.items()})


def _render(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, default=str)
    return str(value)


def deterministic(task, payload):
    if task == 'CUSTOMER_ANALYSIS':
        return _rule_analysis(payload)
    if task == 'MESSAGE_DRAFTING':
        return _rule_message(payload)
    if task == 'REPLY_CLASSIFICATION':
        return _rule_classify(payload)
    return {}


def _rule_analysis(payload):
    context = payload.get('context') if isinstance(payload.get('context'), dict) else payload
    days = int(context.get('days_since_last_order') or 0)
    orders = int(context.get('order_count') or 0)
    cap = int(context.get('discount_cap') or 0)

    discount = 0
    if days >= 60 and orders >= 2:
        discount = min(15, cap)

    return {
        'should_follow_up': True,
        'reason': (
            f'Inactive {days} days with {orders} previous order(s); '
            + ('a retention incentive is justified.' if discount else 'a plain nudge should suffice.')
        ),
        'discount_pct': discount,
        'urgency': 'high' if days >= 90 else 'normal',
        'tone': 'warm and personal',
    }


def _rule_message(payload):
    name = payload.get('customer_name') or 'there'
    product = payload.get('product_name') or 'your usual order'
    discount = int(payload.get('discount_pct') or 0)
    language = (payload.get('language') or 'en').lower()

    if language.startswith('hi'):
        message = f'नमस्ते {name} जी, आपका पिछला ऑर्डर ({product}) बहुत पसंद आया था। '
        if discount:
            message += f'अगली बार के लिए {discount}% की छूट तैयार है। '
        message += 'ऑर्डर करना चाहेंगे?'
    else:
        message = f'Hi {name}, hope you are doing well. Your last order ({product}) '
        if discount:
            message += f'earned you {discount}% off on your next one. '
        else:
            message += 'is something we can get ready for you again. '
        message += 'Would you like to reorder?'

    return {'message': message[:300]}


_NEGATIVE_HINTS = (
    'not interested', 'no thanks', 'no thank', 'stop', 'unsubscribe', 'do not message',
    'dont message', "don't message", 'remove me', 'busy', 'later', 'no need',
)
_POSITIVE_HINTS = (
    'yes', 'sure', 'ok', 'okay', 'send', 'order', 'want', 'please', 'confirm', 'haan',
    'ha', 'thik', 'good', 'interested',
)


def _rule_classify(payload):
    reply = str(payload.get('reply') or '').lower()

    if any(hint in reply for hint in _NEGATIVE_HINTS):
        intent = 'NEGATIVE'
    elif any(hint in reply for hint in _POSITIVE_HINTS):
        intent = 'POSITIVE'
    else:
        intent = 'QUESTION'

    return {
        'intent': intent,
        'summary': (payload.get('reply') or '')[:180],
        'wants_no_contact': any(
            hint in reply for hint in ('stop', 'unsubscribe', 'do not message', 'remove me')
        ),
    }
