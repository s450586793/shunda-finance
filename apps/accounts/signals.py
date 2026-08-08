from django.contrib.auth.models import Group
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from .roles import Role


@receiver(post_migrate)
def create_role_groups(**kwargs):
    for role in Role:
        Group.objects.get_or_create(name=role.value)
