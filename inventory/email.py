from django.core.mail import EmailMultiAlternatives
from django.conf import settings

def notify_head(subject, message):
    """
    Sends HTML email to inventory head
    """

    to_email = ["pdprakhar03@gmail.com"]  # change if needed

    email = EmailMultiAlternatives(
        subject=subject,
        body="This email requires an HTML-compatible email client.",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to_email,
    )

    # ✅ THIS LINE MAKES HTML RENDER
    email.attach_alternative(message, "text/html")

    email.send(fail_silently=False)
