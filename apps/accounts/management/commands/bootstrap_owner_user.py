from django.core.management.base import BaseCommand

from apps.accounts.bootstrap import add_bootstrap_arguments, bootstrap_role_user
from apps.accounts.roles import Role

PASSWORD_ENV = "BOOTSTRAP_OWNER_PASSWORD"
USERNAME_ENV = "BOOTSTRAP_OWNER_USERNAME"


class Command(BaseCommand):
    help = "幂等创建或授权初始老板用户"

    def add_arguments(self, parser):
        add_bootstrap_arguments(parser)

    def handle(self, *args, **options):
        return bootstrap_role_user(
            role=Role.OWNER,
            username_env=USERNAME_ENV,
            password_env=PASSWORD_ENV,
            audit_action="owner_user.bootstrapped",
            options=options,
            stdout=self.stdout,
            password_from_stdin_only=True,
            strict_username=True,
            reject_other_business_role=True,
        )
