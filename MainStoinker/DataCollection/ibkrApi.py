from ib_insync import *
import time
import pandas as pd
import numpy as np
from MainStoinker.NeatTools.decorators import singleton

@singleton
class ibkrApi(IB):
    pass
    #unique id so find trades that have been placed by this algo
    def __init__(self):
        super().__init__()
        self.connect('127.0.0.1', 7497, clientId=124)
        print("Connecting ib_insync")
        time.sleep(5)
        if(not self.isConnected()):
            exit()
            

    


    def orderfilled(trade, fill):
        print("------------------order has been filled")
        print(trade)
        print(fill)

    def close_position(self, position, price = 0, percent=1.00):
        position.contract.exchange = 'SMART'
        numShares = round(percent * position.position)
        if numShares == 0:
            return   
        if price == 0:
            sellOrder = MarketOrder('SELL', numShares)

        else:
            sellOrder = LimitOrder('SELL', numShares, price)

        sell = self.placeOrder(position.contract,sellOrder)
        print(sell)
        sell.fillEvent += self.orderfilled

    def open_position(self, ticker, strike, date, direction, quantity, price = 0):
        #ticker: 'AAPL'
        #strike: int
        #date: '20210430' = 'YYYYMMDD'
        #direction: 'C' or 'P'
        #quantity: int
        #price: float
        if quantity == 0:
            return
        call_option = Option(symbol = ticker,lastTradeDateOrContractMonth = date, strike=strike, right = direction, exchange='SMART', currency='USD')
        if price == 0:
            buyOrder = MarketOrder('BUY', quantity) 

        else:
            buyOrder = LimitOrder('BUY', quantity, price)

        trade = self.placeOrder(call_option,buyOrder)

    #stock version of openPosition
    def simple_buy(self, ticker, quantity, price = 0):
        #ticker: 'AAPL'
        #strike: int
        #date: '20210430' = 'YYYYMMDD'
        #direction: 'C' or 'P'
        #quantity: int
        #price: float
        if quantity == 0:
            return
        stock_contract = Stock(symbol = ticker, exchange = 'SMART', currency= 'USD')
        if price == 0:
            buyOrder = MarketOrder('BUY', quantity) 

        else:
            buyOrder = LimitOrder('BUY', quantity, price)

        trade = self.placeOrder(stock_contract,buyOrder)
        print("PLACED BUY ORDER!")
        return trade

    def simple_sell(self, position, price = 0, percent=1.00):
        position.contract.exchange = 'SMART'
        numShares = round(percent * position.position)
        if numShares == 0:
            return   
        if price == 0:
            sellOrder = MarketOrder('SELL', numShares)

        else:
            sellOrder = LimitOrder('SELL', numShares, price)

        sell = self.placeOrder(position.contract,sellOrder)
        print(sell)
        sell.fillEvent += self.orderfilled

    #Generate new list of positions
    def refresh(self):
        positions = self.positions()
        return positions