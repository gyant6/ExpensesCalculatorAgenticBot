import httpx
import pytest
import respx

from src.bot.tools.fx import get_sgd_exchange_rates

from httpx import Response


@respx.mock
async def test_get_sgd_exchange_rates_sucessful():
    query_parameters = {'base': 'SGD'}
    fx_url = "https://api.fxratesapi.com/latest"
    mock_rate = {"JPY": 124.1}
    respx.get(fx_url, params=query_parameters).mock(return_value=Response(200, json={"success": True, "rates": mock_rate}))
    res = await get_sgd_exchange_rates()
    
    assert mock_rate == res


@respx.mock
async def test_get_sgd_exchange_rates_raises_on_http_error():
    query_parameters = {'base': 'SGD'}
    fx_url = "https://api.fxratesapi.com/latest"
    respx.get(fx_url, params=query_parameters).mock(return_value=Response(500))
    
    with pytest.raises(httpx.HTTPStatusError):
        await get_sgd_exchange_rates()

@respx.mock
async def test_get_sgd_exchange_rates_raises_on_unsuccessful_response():
    query_parameters = {'base': 'SGD'}
    fx_url = "https://api.fxratesapi.com/latest"
    respx.get(fx_url, params=query_parameters).mock(return_value=Response(200, json={"success": False}))
    
    with pytest.raises(RuntimeError):
        await get_sgd_exchange_rates()