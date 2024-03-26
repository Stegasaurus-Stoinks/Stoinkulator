from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import *

def buy_order_object(quantity, limitPrice = None):
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

def sell_order_object(quantity, limitPrice = None):
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


def create_stock_contract(ticker: str):
    contract = Contract()
    contract.symbol = ticker
    contract.secType = 'STK'
    contract.exchange = 'SMART'
    contract.currency = "USD"
    contract.primaryExchange = "SMART"

    return contract


def create_crypto_contract(ticker: str):
    contract = Contract()
    contract.secType = "CRYPTO"
    contract.symbol = ticker
    contract.currency = "USD"
    contract.exchange = "PAXOS"
    contract.primaryExchange = "PAXOS"
    return contract
