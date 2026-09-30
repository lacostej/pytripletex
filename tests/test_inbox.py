"""Tests for the voucher inbox item model."""

from tripletex.endpoints.inbox import InboxItem


def test_registration_flags_are_typed():
    item = InboxItem.model_validate(
        {"id": 1, "canBeRegisteredAsIncomingInvoice": True, "canBeRegisteredAsSimpleInvoice": False}
    )
    assert item.can_be_registered_as_incoming_invoice is True
    assert item.can_be_registered_as_simple_invoice is False
    assert not item.model_extra


def test_registration_flags_default_to_false():
    item = InboxItem.model_validate({"id": 1})
    assert item.can_be_registered_as_incoming_invoice is False
    assert item.can_be_registered_as_simple_invoice is False
