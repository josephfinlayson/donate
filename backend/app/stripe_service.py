import os
import stripe

stripe.api_key = os.getenv("STRIPE")

SITE_URL = os.getenv("SITE_URL", "http://localhost:3000")


async def create_donation_checkout(session_id: str) -> dict:
    """Create a Stripe Checkout Session for a donation."""
    checkout = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Donation to GiveDirectly"},
                    "unit_amount": 500,  # $5 default
                },
                "quantity": 1,
                "adjustable_quantity": {
                    "enabled": True,
                    "minimum": 1,
                    "maximum": 100,
                },
            }
        ],
        metadata={"chat_session_id": session_id},
        success_url=f"{SITE_URL}/thanks?session_id={session_id}",
        cancel_url=f"{SITE_URL}/?returned=true",
    )
    return {"checkout_url": checkout.url, "checkout_session_id": checkout.id}
