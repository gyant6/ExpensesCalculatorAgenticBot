"""Fetches live SGD exchange rates from api.fxratesapi.com."""

import httpx
from pydantic import BaseModel

FX_URL = "https://api.fxratesapi.com/latest"
FX_BASE_CURRENCY = "SGD"
FX_TIMEOUT_SECONDS = 2


class FxRatesResponse(BaseModel):
    success: bool
    rates: dict[str, float] | None = None


def get_sgd_exchange_rates() -> dict[str, float]:
    """Fetch live exchange rates from api.fxratesapi.com with SGD as the base currency.

    Synchronous by design: the only caller is the `end_trip` tool, which LangGraph
    executes inside a synchronous `graph.invoke` and therefore cannot await.

    Returns:
        Dict mapping currency codes to their exchange rate relative to SGD
        (e.g. {'USD': 0.74, 'JPY': 82.3, 'MYR': 3.47, ...}). A rate of R means
        1 SGD = R units of that currency.

    Raises:
        httpx.HTTPStatusError: If the API returns a non-2xx HTTP status.
        httpx.RequestError: If the request never completes (timeout, DNS, connection).
        RuntimeError: If the API returns a 2xx response with success=false, or with
            success=true but no rates field.
        pydantic.ValidationError: If the API response does not match the expected schema.
    """
    with httpx.Client() as client:
        response = client.get(
            FX_URL,
            params={"base": FX_BASE_CURRENCY},
            timeout=FX_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        json_res = FxRatesResponse.model_validate(response.json())

    if not json_res.success:
        raise RuntimeError(f"Failed to fetch exchange rates: {json_res}")
    if json_res.rates is None:
        raise RuntimeError("API returned success=True but rates field is missing")

    return json_res.rates
