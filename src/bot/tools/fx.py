"""Fetches live SGD exchange rates from api.fxratesapi.com."""

import httpx
from pydantic import BaseModel


class FxRatesResponse(BaseModel):
    success: bool
    rates: dict[str, float] | None = None


async def get_sgd_exchange_rates() -> dict[str, float]:
    """Fetch live exchange rates from api.fxratesapi.com with SGD as the base currency.

    Returns:
        Dict mapping currency codes to their exchange rate relative to SGD
        (e.g. {'USD': 0.74, 'JPY': 82.3, 'MYR': 3.47, ...}).

    Raises:
        httpx.HTTPStatusError: If the API returns a non-2xx HTTP status.
        RuntimeError: If the API returns a 2xx response but with success=false in the body.
        pydantic.ValidationError: If the API response does not match the expected schema.
    """
    query_parameters = {"base": "SGD"}
    fx_url = "https://api.fxratesapi.com/latest"

    async with httpx.AsyncClient() as client:
        response = await client.get(fx_url, params=query_parameters, timeout=2)
        response.raise_for_status()
        json_res = FxRatesResponse.model_validate(response.json())

        if not json_res.success:
            raise RuntimeError(f"Failed to fetch exchange rates: {json_res}")
        if json_res.rates is None:
            raise RuntimeError("API returned success=True but rates field is missing")
        return json_res.rates
