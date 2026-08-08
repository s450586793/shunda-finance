import os
import shutil
import sys
from pathlib import Path

from config.e2e_paths import validate_e2e_paths

ROOT = Path(__file__).parents[2].resolve()
E2E_PATHS = validate_e2e_paths(ROOT)
RESULTS_ROOT = E2E_PATHS.results_root
DATABASE_PATH = E2E_PATHS.database
MEDIA_ROOT = E2E_PATHS.media
STATIC_ROOT = E2E_PATHS.static

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.e2e"
os.environ["COMPANY_TAX_ID"] = "91320281TEST000001"
os.environ["E2E_DATABASE_PATH"] = str(DATABASE_PATH)
os.environ["E2E_MEDIA_ROOT"] = str(MEDIA_ROOT)
os.environ["E2E_STATIC_ROOT"] = str(STATIC_ROOT)


def _validate_e2e_settings(paths):
    from django.conf import settings

    expected_paths = {
        "results": paths.results_root,
        "database": paths.database,
        "media": paths.media,
        "static": paths.static,
    }
    actual_paths = {
        "results": Path(settings.E2E_RESULTS_ROOT),
        "database": Path(settings.DATABASES["default"]["NAME"]),
        "media": Path(settings.MEDIA_ROOT),
        "static": Path(settings.STATIC_ROOT),
    }
    if os.environ["DJANGO_SETTINGS_MODULE"] != "config.settings.e2e":
        raise RuntimeError("E2E runner must use config.settings.e2e")
    if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
        raise RuntimeError("E2E runner must use SQLite")
    for name, expected_path in expected_paths.items():
        if actual_paths[name] != expected_path:
            raise RuntimeError(f"E2E {name} path validation failed")


def prepare_database():
    paths = validate_e2e_paths(ROOT)
    import django

    django.setup()
    _validate_e2e_settings(paths)
    paths.results_root.mkdir(parents=True, exist_ok=True)
    paths = validate_e2e_paths(ROOT)
    _validate_e2e_settings(paths)
    paths.database.unlink(missing_ok=True)
    shutil.rmtree(paths.media, ignore_errors=True)
    paths.media.mkdir(parents=True)

    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    from apps.accounts.roles import Role, assign_role
    from apps.ledger.choices import MoneyChannel
    from apps.ledger.models import FundingAccount
    from apps.parties.models import Counterparty

    call_command("migrate", interactive=False, verbosity=0)
    user_model = get_user_model()
    finance = user_model.objects.create_user("finance-e2e", password="finance-e2e")
    owner = user_model.objects.create_user("owner-e2e", password="owner-e2e")
    assign_role(finance, Role.FINANCE)
    assign_role(owner, Role.OWNER)
    Counterparty.objects.create(
        name="测试铁路物流收款专户",
        normalized_name="测试铁路物流收款专户",
        tax_id="91310000TEST000001",
        is_supplier=True,
    )
    FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="脱敏农行结算账户",
        identifier="TEST-BANK-RAIL-0001",
        masked_identifier="*********0001",
    )


if __name__ == "__main__":
    prepare_database()
    if "--setup-only" in sys.argv[1:]:
        raise SystemExit(0)
    from django.core.management import execute_from_command_line

    execute_from_command_line(
        [sys.argv[0], "runserver", "127.0.0.1:8015", "--noreload"]
    )
