from django.db import migrations, models

PENDING, APPROVED, DECLINED, PAID = "PENDING", "APPROVED", "DECLINED", "PAID"


class Migration(migrations.Migration):
    """
    §7: FundRequest.approved_by/approved_at -> decided_by/decided_at (they are set on a DECLINED
    request too), plus decision_reason. RenameField, not remove/add, so existing values are kept.

    Existing DECLINED rows kept their reason only inside ``notes``; the new constraint requires
    decision_reason on every DECLINED row, so those rows are back-filled first.
    """

    dependencies = [
        ("approvals", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="fundrequest", name="approvals_fundrequest_status_consistent"),
        migrations.RenameField(model_name="fundrequest", old_name="approved_by", new_name="decided_by"),
        migrations.RenameField(model_name="fundrequest", old_name="approved_at", new_name="decided_at"),
        migrations.AddField(
            model_name="fundrequest",
            name="decision_reason",
            field=models.TextField(
                blank=True, default="", editable=False,
                help_text="The MD's reason. Required when declining: the PM is told it.",
            ),
            preserve_default=False,
        ),
        # Back-fill from the "Declined by the Managing Director: <reason>" line decline() used to
        # append to notes. Raw SQL: queryset.update() is refused on audited models (R7).
        migrations.RunSQL(
            sql=(
                "UPDATE approvals_fundrequest SET decision_reason = COALESCE(NULLIF(TRIM(substring(notes "
                "from 'Declined by the Managing Director: (.*)$')), ''), 'Reason not recorded (before 0002).') "
                "WHERE status = 'DECLINED' AND decision_reason = ''"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddConstraint(
            model_name="fundrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(status=PENDING, decided_by__isnull=True, decided_at__isnull=True, decision_reason="",
                             paid_by__isnull=True, paid_at__isnull=True, payment_reference="")
                    | models.Q(status__in=[APPROVED, DECLINED], decided_by__isnull=False, decided_at__isnull=False,
                               paid_by__isnull=True, paid_at__isnull=True, payment_reference="")
                    | (models.Q(status=PAID, decided_by__isnull=False, decided_at__isnull=False,
                                paid_by__isnull=False, paid_at__isnull=False) & ~models.Q(payment_reference=""))
                ),
                name="approvals_fundrequest_status_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="fundrequest",
            constraint=models.CheckConstraint(
                condition=~models.Q(status=DECLINED) | ~models.Q(decision_reason=""),
                name="approvals_fundrequest_decline_has_reason",
            ),
        ),
    ]
