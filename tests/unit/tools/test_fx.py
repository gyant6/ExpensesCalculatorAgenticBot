import httpx
import pytest
import respx
from httpx import Response

from src.bot.tools.fx import FX_URL, get_sgd_exchange_rates

QUERY_PARAMETERS = {"base": "SGD"}


@respx.mock
def test_get_sgd_exchange_rates_sucessful() -> None:
    mock_rate = {"JPY": 124.1}
    respx.get(FX_URL, params=QUERY_PARAMETERS).mock(
        return_value=Response(200, json={"success": True, "rates": mock_rate})
    )
    res = get_sgd_exchange_rates()

    assert mock_rate == res


@respx.mock
def test_get_sgd_exchange_rates_raises_on_http_error() -> None:
    respx.get(FX_URL, params=QUERY_PARAMETERS).mock(return_value=Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        get_sgd_exchange_rates()


@respx.mock
def test_get_sgd_exchange_rates_raises_on_unsuccessful_response() -> None:
    respx.get(FX_URL, params=QUERY_PARAMETERS).mock(
        return_value=Response(200, json={"success": False})
    )

    with pytest.raises(RuntimeError):
        get_sgd_exchange_rates()


@respx.mock
def test_get_sgd_exchange_rates_raises_when_rates_field_missing() -> None:
    respx.get(FX_URL, params=QUERY_PARAMETERS).mock(
        return_value=Response(200, json={"success": True})
    )

    with pytest.raises(RuntimeError):
        get_sgd_exchange_rates()
