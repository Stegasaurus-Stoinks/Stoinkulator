from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import threading
import time


class IBapi(EWrapper, EClient):
	def __init__(self):
		EClient.__init__(self, self)
                
	def tickPrice(self, reqId, tickType, price, attrib):
		if tickType == 2 and reqId == 1:
			print('The current ask price is: ', price)
                        
    def nextValidId(self, orderId:int):
        print("Setting nextValidOrderId: %d", orderId)
        self.nextValidOrderId = orderId
        self.startData("AAPL")

