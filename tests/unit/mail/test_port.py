from email_agent.mail.port import EmailProvider


def test_email_provider_has_required_methods():
    for name in ("verify_webhook", "parse_inbound", "send_reply"):
        assert hasattr(EmailProvider, name)
