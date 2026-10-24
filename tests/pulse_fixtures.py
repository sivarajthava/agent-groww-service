"""Fixed pulse inputs shared by the render, generate and validate tests (synthetic, PII-free)."""

from datetime import date

from conftest import make_review
from groww_pulse.models import PulseDraft, Quote, Theme

WEEK = "2026-W43"
WINDOW = (date(2026, 8, 3), date(2026, 10, 25))
TOTAL, AVG = 2144, 2.98

REVIEWS = [
    make_review("r01", "Customer care never replies to my ticket. It has been ten days and my account is still blocked.", 1),
    make_review("r02", "Support is very slow, raised three tickets about KYC and got no answer for a week", 1),
    make_review("r03", "After the latest update the chart opens a scalper option and the back button disappears from the screen", 1),
    make_review("r04", "Charts freeze during market hours since the new update, please fix it soon", 2),
    make_review("r05", "Brokerage charges are too high compared to other apps for small investors like me", 2),
    make_review("r06", "Hidden charges on every order, the annual fee was deducted without any clear notice", 1),
    make_review("r07", "Please call me on [phone] about the refund, support has not helped at all", 1),
]
BY_ID = {r.id: r for r in REVIEWS}

TOP = [
    Theme("customer-support-delays", "Customer Support Delays", "Tickets go unanswered for days.",
          ("r01", "r02", "r07"), 415, 0.19, 1.6, 0.81, None),
    Theme("chart-bugs-after-update", "Chart Bugs After Update", "Charts misbehave since the latest update.",
          ("r03", "r04"), 262, 0.12, 2.6, 0.51, 0.03),
    Theme("high-brokerage-charges", "High Brokerage Charges", "Fees feel high and unclear.",
          ("r05", "r06"), 210, 0.098, 2.1, 0.66, None),
]
QUOTES = [
    Quote("Customer care never replies to my ticket. It has been ten days and my account is still blocked.", 1,
          "r01", "customer-support-delays"),
    Quote("After the latest update the chart opens a scalper option and the back button disappears from the screen",
          1, "r03", "chart-bugs-after-update"),
    Quote("Hidden charges on every order, the annual fee was deducted without any clear notice", 1,
          "r06", "high-brokerage-charges"),
]
DRAFT = PulseDraft(
    theme_summaries=(
        "Users wait days for replies on account, KYC and refund tickets.",
        "Since the latest update, charts lag and the back button disappears.",
        "Small investors find brokerage and annual fees high and poorly explained.",
    ),
    actions=(
        "Customer Support Delays: publish a 48-hour first-response target for tickets and track it weekly.",
        "Chart Bugs After Update: ship a hotfix restoring the back button and make scalper mode opt-in.",
        "High Brokerage Charges: show a full fee breakdown before every order and in the annual statement.",
    ),
)
DRAFT_JSON = {"theme_summaries": list(DRAFT.theme_summaries), "actions": list(DRAFT.actions)}
RENDER_ARGS = dict(week=WEEK, window=WINDOW, total_reviews=TOTAL, avg_rating=AVG, top=TOP, quotes=QUOTES)
