"""
Your stores. Add or edit stores here. Each one gets its own Google Wallet card
template (class) and its own branded enrollment page at /store/<id>.

For the demo there are two stores, but we're only building the deeper features
(stamp card, checkout) on the first one for now. Both enrollment pages work.

Fields:
    id            short slug, letters/numbers only. Used in URLs and the wallet
                  class id. Don't change it once customers exist for that store.
    name          store name shown on the card and page
    program_name  the loyalty program name shown on the card
    logo_url      public HTTPS PNG logo (Google masks it into a circle)
    brand_color   hex colour for the card background and the page accent
    reward_goal   purchases needed for a reward (used later, in the stamp step)
    reward_text   short description of the reward
"""

STORES = [
    {
        "id": "brewbar",
        "name": "Brew Bar Coffee",
        "program_name": "Brew Bar Rewards",
        "logo_url": "https://example.com/brewbar-logo.png",
        "brand_color": "#3b2a20",
        "reward_goal": 5,
        "reward_text": "A free coffee with every 5 purchases",
    },
    {
        "id": "angkormart",
        "name": "Angkor Mart",
        "program_name": "Angkor Advantage",
        "logo_url": "https://example.com/angkormart-logo.png",
        "brand_color": "#0e7c86",
        "reward_goal": 10,
        "reward_text": "Member discounts on every visit",
    },
]


def get_store(store_id: str):
    """Return the store dict for an id, or None if it doesn't exist."""
    for store in STORES:
        if store["id"] == store_id:
            return store
    return None
