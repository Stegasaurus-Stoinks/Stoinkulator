from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import BarData
from ibapi.common import *

def buyOrderObject(quantity, limitPrice = None):
    order = Order()
    order.action = "Buy"
    order.totalQuantity = quantity
    if limitPrice == None:
        order.orderType =  "MKT"
    else:
        order.orderType = "LMT"
        order.lmtPrice = limitPrice
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    # order.adjustedStopLimitPrice = stopPrice

    return order

def sellOrderObject(quantity, limitPrice = None):
    order = Order()
    order.action = "Sell"
    order.totalQuantity = quantity
    if limitPrice == None:
        order.orderType =  "MKT"
    else:
        order.orderType = "LMT"
        order.lmtPrice = limitPrice
    order.eTradeOnly = False
    order.firmQuoteOnly = False

    return order

def makeStockContract(tickerSymbol: str):
    contract = Contract()
    contract.symbol = tickerSymbol
    contract.secType = 'STK'
    contract.exchange = 'SMART'
    contract.currency = "USD"
    contract.primaryExchange = "SMART"

    return contract