"""
Audit every delete of an Auditable model (R7), however it happens:
instance.delete(), queryset.delete(), admin "delete selected", or a cascade
from a parent row. Django sends post_delete for each row in all of those
cases; the deletion and this entry share the collector's transaction.
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .audit import get_current_actor
from .models import AuditAction, AuditEntry, Auditable


@receiver(post_delete, dispatch_uid="core.audit_delete")
def audit_delete(sender, instance, using, **kwargs):
    if not isinstance(instance, Auditable):
        return
    AuditEntry.objects.using(using).create(
        actor=get_current_actor(),
        action=AuditAction.DELETE,
        model=instance._meta.label,
        object_id=str(instance.pk),
        object_repr=str(instance)[:255],
        changes=instance._audit_redact(
            {k: [v, None] for k, v in instance._audit_snapshot().items()}
        ),
    )
