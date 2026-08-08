from django.core.management.base import BaseCommand

from apps.accounts.bootstrap import add_bootstrap_arguments, bootstrap_role_user
from apps.accounts.roles import Role

PASSWORD_ENV = "BOOTSTRAP_FINANCE_PASSWORD"
USERNAME_ENV = "BOOTSTRAP_FINANCE_USERNAME"
class Command(BaseCommand):
    help = "幂等创建或授权初始财务用户"

    def add_arguments(self, parser):
        add_bootstrap_arguments(parser)

    def handle(self, *args, **options):
        return bootstrap_role_user(
            role=Role.FINANCE,
            username_env=USERNAME_ENV,
            password_env=PASSWORD_ENV,
            audit_action="finance_user.bootstrapped",
            options=options,
            stdout=self.stdout,
        )
