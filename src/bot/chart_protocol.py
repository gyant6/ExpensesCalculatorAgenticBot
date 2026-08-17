"""Payload contract between the main Lambda and the chart Lambda.

Deliberately dependency-free. Both sides import it, and the chart function must not
acquire the bot's configuration, secrets or Telegram dependencies just to agree on the
spelling of a dictionary key.
"""

from typing import Final

# Request, written by charts_client and read by chart_handler.
REQUEST_EXPENSES: Final = "expenses"
REQUEST_FX_RATES: Final = "fx_rates"

# Response, written by chart_handler and read by charts_client. Base64 because a Lambda
# response is JSON and cannot carry raw bytes.
RESPONSE_PIE_PNG: Final = "pie_png_base64"
RESPONSE_BAR_PNG: Final = "bar_png_base64"
