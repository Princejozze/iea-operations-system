from django.db import migrations, models


class Migration(migrations.Migration):
    """Tender.bid_opening_date: empty means opened at the submission deadline (Tender.opening_at)."""

    dependencies = [
        ("tenders", "0005_extraction_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="tender",
            name="bid_opening_date",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="When bids are publicly opened. Leave empty if at the submission deadline.",
            ),
        ),
        migrations.AddConstraint(
            model_name="tender",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(bid_opening_date__isnull=True)
                    | models.Q(submission_deadline__isnull=True)
                    | models.Q(bid_opening_date__gte=models.F("submission_deadline"))
                ),
                name="tenders_bid_opening_not_before_submission",
            ),
        ),
    ]
