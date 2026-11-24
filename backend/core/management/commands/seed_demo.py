"""``python manage.py seed_demo`` — load the synthetic dataset."""
import json

from django.core.management.base import BaseCommand

from core import seed


class Command(BaseCommand):
    help = 'Create the synthetic customer/purchase dataset (no real data is used).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Delete all existing data and checkpoints before seeding.',
        )

    def handle(self, *args, **options):
        summary = seed.seed_demo(reset=options['reset'])
        self.stdout.write(json.dumps(summary, indent=2))
        self.stdout.write(self.style.SUCCESS('Synthetic data ready.'))
