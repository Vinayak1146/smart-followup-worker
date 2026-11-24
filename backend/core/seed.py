"""Synthetic dataset.

No real customer data is used anywhere in this prototype. This module builds a
small, deterministic shop history whose *shape* is the point: some customers are
win-back candidates, some are not, and each reason for not being one is a
different rule. That makes the worker's decisions checkable rather than merely
plausible.

The dataset is deliberately built so each evaluation scenario has an obvious
subject:

===================  ==========================================================
Customer             What it exercises
===================  ==========================================================
Priya Nair           Eligible, no incentive justified → autonomous success
Meera Joshi          Eligible, then declines → recorded, never repeated
Rahul Verma          Repeat buyer, 15% proposed → owner approval required
Vikram Mehta         Hindi speaker with a memory note → approval + language
Nisha Kulkarni       Declined 10 days ago → cooldown blocks re-contact
Arjun Singh          Ordered 12 days ago → below the inactivity floor
Fatima Sheikh        No marketing consent → outreach not permitted
Deepak Rao           Inactive 240 days → beyond the win-back window
Sneha Iyer           Opted out → blocked at policy level
===================  ==========================================================

Beyond those nine, ``EXTRA_CUSTOMERS`` adds a larger pool of ordinary profiles so
the customer list and the pickers reflect a real shop's volume rather than a
handful of fixtures. They are not inert: each one carries a purchase dated to a
specific point in the eligibility window, so some are workable and some are
correctly refused.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    AgentMemory,
    Approval,
    Customer,
    FollowUp,
    Interaction,
    Purchase,
    RunStep,
    SimulationControl,
    WorkerRun,
)

DATASET = [
    {
        'name': 'Priya Nair',
        'phone': '+919810010001',
        'city': 'Mumbai',
        'language': 'en',
        'channel': Customer.Channel.WHATSAPP,
        'consent': True,
        'status': Customer.Status.ACTIVE,
        'note': 'Eligible with no incentive justified — the autonomous success case.',
        'orders': [
            ('Muslin Cotton Kurta', 'Apparel', 1, '1299.00', 45),
        ],
    },
    {
        'name': 'Meera Joshi',
        'phone': '+919810010002',
        'city': 'Bengaluru',
        'language': 'en',
        'channel': Customer.Channel.WHATSAPP,
        'consent': True,
        'status': Customer.Status.ACTIVE,
        'note': 'Eligible — the subject of the customer-rejection scenario.',
        'orders': [
            ('Cold Pressed Coconut Oil 1L', 'Grocery', 2, '780.00', 50),
        ],
    },
    {
        'name': 'Rahul Verma',
        'phone': '+919810010003',
        'city': 'Pune',
        'language': 'en',
        'channel': Customer.Channel.WHATSAPP,
        'consent': True,
        'status': Customer.Status.LAPSED,
        'note': (
            'Repeat buyer inactive 75 days — the analysis justifies 15% off, which '
            'crosses the autonomous limit and needs the owner.'
        ),
        'orders': [
            ('Blue Denim Jacket', 'Apparel', 1, '2499.00', 75),
            ('Blue Denim Jacket', 'Apparel', 1, '2499.00', 140),
            ('White Sneakers', 'Footwear', 1, '1899.00', 210),
        ],
    },
    {
        'name': 'Vikram Mehta',
        'phone': '+919810010004',
        'city': 'Jaipur',
        'language': 'hi',
        'channel': Customer.Channel.WHATSAPP,
        'consent': True,
        'status': Customer.Status.LAPSED,
        'note': (
            'Hindi speaker with a stored preference — shows memory recall and '
            'language-aware drafting under an approval.'
        ),
        'orders': [
            ('Handloom Cotton Bedsheet', 'Home', 1, '1650.00', 95),
            ('Handloom Cotton Bedsheet', 'Home', 1, '1650.00', 180),
        ],
    },
    {
        'name': 'Nisha Kulkarni',
        'phone': '+919810010005',
        'city': 'Nagpur',
        'language': 'en',
        'channel': Customer.Channel.SMS,
        'consent': True,
        'status': Customer.Status.ACTIVE,
        'note': 'Declined an offer 10 days ago — the cooldown rule must refuse to re-contact.',
        'orders': [
            ('Terracotta Dinner Set', 'Home', 1, '2100.00', 70),
            ('Terracotta Dinner Set', 'Home', 1, '2100.00', 130),
        ],
        'prior_follow_up': {
            'status': FollowUp.Status.DECLINED,
            'discount_pct': 15,
            'product_name': 'Terracotta Dinner Set',
            'days_ago': 10,
            'outcome_note': 'Customer said the timing was not right.',
        },
    },
    {
        'name': 'Arjun Singh',
        'phone': '+919810010006',
        'city': 'Delhi',
        'language': 'en',
        'channel': Customer.Channel.WHATSAPP,
        'consent': True,
        'status': Customer.Status.ACTIVE,
        'note': 'Ordered 12 days ago — too recent to nudge.',
        'orders': [
            ('Cotton Pyjama Set', 'Apparel', 1, '899.00', 12),
        ],
    },
    {
        'name': 'Fatima Sheikh',
        'phone': '+919810010007',
        'city': 'Hyderabad',
        'language': 'en',
        'channel': Customer.Channel.WHATSAPP,
        'consent': False,
        'status': Customer.Status.ACTIVE,
        'note': 'Never consented to marketing — outreach is not permitted.',
        'orders': [
            ('Embroidered Dupatta', 'Apparel', 1, '1150.00', 80),
        ],
    },
    {
        'name': 'Deepak Rao',
        'phone': '+919810010008',
        'city': 'Chennai',
        'language': 'en',
        'channel': Customer.Channel.SMS,
        'consent': True,
        'status': Customer.Status.LAPSED,
        'note': 'Inactive 240 days — beyond the window where one message is honest.',
        'orders': [
            ('Steel Tiffin Carrier', 'Kitchen', 1, '1450.00', 240),
        ],
    },
    {
        'name': 'Sneha Iyer',
        'phone': '+919810010009',
        'city': 'Kochi',
        'language': 'en',
        'channel': Customer.Channel.WHATSAPP,
        'consent': True,
        'status': Customer.Status.OPTED_OUT,
        'note': 'Opted out of outreach — blocked at policy level.',
        'orders': [
            ('Coir Door Mat', 'Home', 2, '640.00', 90),
        ],
    },
]

EXTRA_CUSTOMERS = [
    (
        'Aditya Menon', '+919810010010', 'Thiruvananthapuram', 'ml',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        62, 'Linen Shirt', 'Apparel', '1599.00',
    ),
    (
        'Kavya Reddy', '+919810010011', 'Hyderabad', 'te',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        41, 'Yoga Mat', 'Fitness', '899.00',
    ),
    (
        'Sameer Khan', '+919810010012', 'Lucknow', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        88, 'Leather Wallet', 'Accessories', '1199.00',
    ),
    (
        'Ananya Ghosh', '+919810010013', 'Kolkata', 'bn',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        35, 'Cotton Bedsheet Set', 'Home', '1499.00',
    ),
    (
        'Rohit Patil', '+919810010014', 'Nashik', 'mr',
        Customer.Channel.SMS, True, Customer.Status.LAPSED,
        120, 'Running Shoes', 'Footwear', '2299.00',
    ),
    (
        'Divya Nambiar', '+919810010015', 'Kochi', 'ml',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        150, 'Silk Saree', 'Apparel', '3499.00',
    ),
    (
        'Imran Sheikh', '+919810010016', 'Bhopal', 'hi',
        Customer.Channel.WHATSAPP, False, Customer.Status.ACTIVE,
        55, 'Wireless Earbuds', 'Electronics', '1999.00',
    ),
    (
        'Pooja Sharma', '+919810010017', 'Jaipur', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        18, 'Ceramic Mug Set', 'Kitchen', '749.00',
    ),
    (
        'Karthik Subramanian', '+919810010018', 'Coimbatore', 'ta',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        95, 'Stainless Water Bottle', 'Kitchen', '599.00',
    ),
    (
        'Neha Deshpande', '+919810010019', 'Pune', 'mr',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        47, 'Herbal Face Wash', 'Beauty', '349.00',
    ),
    (
        'Aarav Gupta', '+919810010020', 'Delhi', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        210, 'Desk Lamp', 'Home', '1299.00',
    ),
    (
        'Shreya Banerjee', '+919810010021', 'Kolkata', 'bn',
        Customer.Channel.SMS, True, Customer.Status.ACTIVE,
        66, 'Canvas Tote Bag', 'Accessories', '699.00',
    ),
    (
        'Manish Yadav', '+919810010022', 'Kanpur', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        78, 'Formal Trousers', 'Apparel', '1699.00',
    ),
    (
        'Lakshmi Pillai', '+919810010023', 'Chennai', 'ta',
        Customer.Channel.WHATSAPP, True, Customer.Status.OPTED_OUT,
        100, 'Brass Lamp', 'Home', '1850.00',
    ),
    (
        'Ravi Kumar', '+919810010024', 'Patna', 'hi',
        Customer.Channel.SMS, True, Customer.Status.ACTIVE,
        33, 'Almond Oil 500ml', 'Grocery', '550.00',
    ),
    (
        'Sneha Chatterjee', '+919810010025', 'Guwahati', 'bn',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        140, 'Woollen Sweater', 'Apparel', '1999.00',
    ),
    (
        'Vikas Jain', '+919810010026', 'Indore', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        52, 'Kitchen Knife Set', 'Kitchen', '1450.00',
    ),
    (
        'Meghana Rao', '+919810010027', 'Mysuru', 'kn',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        71, 'Scented Candle Set', 'Home', '899.00',
    ),
    (
        'Faisal Ahmed', '+919810010028', 'Hyderabad', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        25, 'Peanut Butter 1kg', 'Grocery', '480.00',
    ),
    (
        'Tanvi Joshi', '+919810010029', 'Ahmedabad', 'gu',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        108, 'Handmade Soap Pack', 'Beauty', '399.00',
    ),
    (
        'Suresh Nair', '+919810010030', 'Thiruvananthapuram', 'ml',
        Customer.Channel.SMS, True, Customer.Status.LAPSED,
        165, 'Bamboo Cutting Board', 'Kitchen', '799.00',
    ),
    (
        'Isha Malhotra', '+919810010031', 'Chandigarh', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        58, 'Sports T-shirt', 'Apparel', '899.00',
    ),
    (
        'Ganesh Iyer', '+919810010032', 'Madurai', 'ta',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        44, 'Copper Bottle', 'Kitchen', '1099.00',
    ),
    (
        'Ritu Agarwal', '+919810010033', 'Agra', 'hi',
        Customer.Channel.WHATSAPP, False, Customer.Status.LAPSED,
        130, 'Jute Rug', 'Home', '1399.00',
    ),
    (
        'Nikhil Menon', '+919810010034', 'Kozhikode', 'ml',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        92, 'Backpack', 'Accessories', '1899.00',
    ),
    (
        'Priyanka Saxena', '+919810010035', 'Noida', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        60, 'Masala Gift Box', 'Grocery', '1250.00',
    ),
    (
        'Arvind Krishnan', '+919810010036', 'Salem', 'ta',
        Customer.Channel.SMS, True, Customer.Status.LAPSED,
        175, 'Table Runner', 'Home', '649.00',
    ),
    (
        'Zoya Siddiqui', '+919810010037', 'Aligarh', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        38, 'Embroidered Dupatta', 'Apparel', '1150.00',
    ),
    (
        'Harish Patel', '+919810010038', 'Surat', 'gu',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        85, 'Basmati Rice 5kg', 'Grocery', '899.00',
    ),
    (
        'Deepa Menon', '+919810010039', 'Thrissur', 'ml',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        12, 'Kids Pyjama Set', 'Apparel', '699.00',
    ),
    (
        'Sagar Kulkarni', '+919810010040', 'Kolhapur', 'mr',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        145, 'Wireless Earbuds', 'Electronics', '1999.00',
    ),
    (
        'Nandini Bose', '+919810010041', 'Howrah', 'bn',
        Customer.Channel.SMS, True, Customer.Status.ACTIVE,
        49, 'Cotton Socks Pack', 'Apparel', '349.00',
    ),
    (
        'Rajat Verma', '+919810010042', 'Ghaziabad', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        240, 'Steel Tiffin Carrier', 'Kitchen', '1450.00',
    ),
    (
        'Aishwarya Rajan', '+919810010043', 'Puducherry', 'ta',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        55, 'Coir Door Mat', 'Home', '640.00',
    ),
    (
        'Kabir Malhotra', '+919810010044', 'Amritsar', 'pa',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        115, 'Muslin Cotton Kurta', 'Apparel', '1299.00',
    ),
    (
        'Sarita Devi', '+919810010045', 'Varanasi', 'hi',
        Customer.Channel.SMS, True, Customer.Status.ACTIVE,
        68, 'Handloom Cotton Bedsheet', 'Home', '1650.00',
    ),
    (
        'Vishal Shetty', '+919810010046', 'Mangaluru', 'kn',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        82, 'Blue Denim Jacket', 'Apparel', '2499.00',
    ),
    (
        'Rhea Dsouza', '+919810010047', 'Panaji', 'en',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        40, 'Cold Pressed Coconut Oil 1L', 'Grocery', '780.00',
    ),
    (
        'Mohammed Arif', '+919810010048', 'Srinagar', 'hi',
        Customer.Channel.WHATSAPP, True, Customer.Status.LAPSED,
        155, 'Wool Shawl', 'Apparel', '2199.00',
    ),
    (
        'Pallavi Naik', '+919810010049', 'Bhubaneswar', 'en',
        Customer.Channel.WHATSAPP, True, Customer.Status.ACTIVE,
        29, 'Terracotta Dinner Set', 'Home', '2100.00',
    ),
    (
        'Devendra Singh', '+919810010050', 'Jodhpur', 'hi',
        Customer.Channel.SMS, True, Customer.Status.LAPSED,
        70, 'White Sneakers', 'Footwear', '1899.00',
    ),
]


def _expand_extras():
    """Expand ``EXTRA_CUSTOMERS`` into the same spec shape as the curated set.

    Columns per row: name, phone, city, language, channel, consent, status,
    days_ago, product, category, amount. Each profile carries one purchase so the
    extra volume flows through the real eligibility rules rather than sitting
    inert on the customer list.
    """
    specs = []
    for (
        name,
        phone,
        city,
        language,
        channel,
        consent,
        status,
        days_ago,
        product,
        category,
        amount,
    ) in EXTRA_CUSTOMERS:
        specs.append(
            {
                'name': name,
                'phone': phone,
                'city': city,
                'language': language,
                'channel': channel,
                'consent': consent,
                'status': status,
                'note': f'Synthetic profile — inactive {days_ago} day(s) at seed time.',
                'orders': [(product, category, 1, amount, days_ago)],
            }
        )
    return specs


DATASET = DATASET + _expand_extras()

BUSINESS_MEMORY = [
    ('tone_guide', 'Keep win-back messages warm and specific; never use urgency pressure.'),
    ('offer_policy', 'Discounts above the autonomous limit are granted only for repeat buyers.'),
    ('channel_note', 'WhatsApp converts better than SMS for Apparel; SMS is used for older profiles.'),
]


@transaction.atomic
def seed_demo(*, reset=False):
    """Create the synthetic dataset. Idempotent: customers are keyed by phone."""
    if reset:
        reset_demo()

    SimulationControl.current().reset()

    created = {'customers': 0, 'purchases': 0, 'memories': 0, 'prior_follow_ups': 0}
    scenarios = []

    for spec in DATASET:
        customer, was_created = Customer.objects.get_or_create(
            phone=spec['phone'],
            defaults={
                'name': spec['name'],
                'city': spec['city'],
                'language': spec['language'],
                'preferred_channel': spec['channel'],
                'marketing_consent': spec['consent'],
                'status': spec['status'],
            },
        )
        if not was_created:
            scenarios.append(
                {
                    'customer': customer.name,
                    'customer_id': customer.id,
                    'note': spec['note'],
                    'already_present': True,
                }
            )
            continue

        created['customers'] += 1
        now = timezone.now()
        for product_name, category, quantity, amount, days_ago in spec['orders']:
            Purchase.objects.create(
                customer=customer,
                product_name=product_name,
                category=category,
                quantity=quantity,
                amount=amount,
                purchased_at=now - timedelta(days=days_ago),
            )
            created['purchases'] += 1
        customer.recompute_last_order()

        prior = spec.get('prior_follow_up')
        if prior:
            follow_up = FollowUp.objects.create(
                customer=customer,
                status=prior['status'],
                discount_pct=prior['discount_pct'],
                product_name=prior['product_name'],
                outcome_note=prior['outcome_note'],
            )
            moment = now - timedelta(days=prior['days_ago'])
            FollowUp.objects.filter(pk=follow_up.pk).update(
                created_at=moment, updated_at=moment
            )
            created['prior_follow_ups'] += 1

        scenarios.append(
            {
                'customer': customer.name,
                'customer_id': customer.id,
                'note': spec['note'],
            }
        )

    for key, value in BUSINESS_MEMORY:
        exists = AgentMemory.objects.filter(
            scope=AgentMemory.Scope.BUSINESS, key=key, value=value
        ).exists()
        if not exists:
            AgentMemory.objects.create(scope=AgentMemory.Scope.BUSINESS, key=key, value=value)
            created['memories'] += 1

    return {
        'created': created,
        'customers_total': Customer.objects.count(),
        'scenarios': scenarios,
        'note': 'Synthetic data only — no real customer records are used.',
    }


@transaction.atomic
def reset_demo():
    """Delete all synthetic data, the execution history, and the checkpoints."""
    deleted = {
        'steps': RunStep.objects.count(),
        'runs': WorkerRun.objects.count(),
        'approvals': Approval.objects.count(),
        'follow_ups': FollowUp.objects.count(),
        'interactions': Interaction.objects.count(),
        'purchases': Purchase.objects.count(),
        'memories': AgentMemory.objects.count(),
        'customers': Customer.objects.count(),
    }
    RunStep.objects.all().delete()
    Approval.objects.all().delete()
    FollowUp.objects.all().delete()
    Interaction.objects.all().delete()
    Purchase.objects.all().delete()
    AgentMemory.objects.all().delete()
    WorkerRun.objects.all().delete()
    Customer.objects.all().delete()

    checkpoints = _clear_checkpoints()
    return {'deleted': deleted, 'checkpoints': checkpoints}


def _clear_checkpoints():
    """Drop the durable LangGraph checkpoints along with the data.

    Leaving them behind is worse than it sounds: a checkpoint from a deleted run
    can still be resumed, and the worker would carry on a thread whose customer no
    longer exists.

    The rows are deleted **through the live connection** rather than by removing
    the file. The compiled graph holds that saver, so unlinking the database out
    from under it would leave every later run writing to a closed handle.
    """
    from .agent import graph

    graph.checkpointer()
    connection = graph._connection  # noqa: SLF001 — resetting the module-level singleton
    if connection is None:
        return []

    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if row[0].startswith('checkpoint') and 'migration' not in row[0]
    ]
    for table in tables:
        connection.execute(f'DELETE FROM {table}')
    connection.commit()
    return tables
