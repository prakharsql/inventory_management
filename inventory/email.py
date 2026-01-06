from django.core.mail import send_mail
from django.conf import settings

HEAD_EMAIL = "rudranshkiller@gmail.com"

def notify_head(subject, message):
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[HEAD_EMAIL],
        fail_silently=False,
    )
