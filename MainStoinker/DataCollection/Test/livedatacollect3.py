from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
from csv import writer
import datetime

import numpy as np
import pandas as pd

import threading
import time


class IBapi(EWrapper, EClient):
	def __init__(self):
		EClient.__init__(self, self)
                
	def tickPrice(self, reqId, tickType, price, attrib):
		if tickType == 2 and reqId == 1:
			print('The current ask price is: ', price)

	def nextValidId(self, orderId: int):
		print("Setting nextValidOrderId: %d", orderId)
		self.nextValidOrderId = orderId
		self.startData("AAPL")

	def historicalData(self, reqId: int, bar: BarData):
		# print("HistoricalData. ReqId:", reqId, "BarData.", bar)

		candleData = [bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
		self.df = self.df.append([candleData], ignore_index=True)

	# print(candleData)
	# print(self.test)
	# np.append(self.dataArray,[candleData])

	def historicalDataEnd(self, reqId: int, start: str, end: str):
		print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)

		print(self.df)
		f = open('./MainStoinker/DataCollection/Test/datalive.csv', 'w+')
		self.df.to_csv(f, index=False, header=False, lineterminator='\n')
		f.close()
		print("All Historical Data Collected: Live Data Starting Now...")
		self.disconnect()

	# print("Finished")

	def historicalDataUpdate(self, reqId: int, bar: BarData):
		global lastbar
		if lastbar != 0:
			if (bar.average != lastbar.average):
				print("HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)

				# Open our existing CSV file in append mode
				# Create a file object for this file
				candleData = [bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
				with open('./MainStoinker/DataCollection/Test/datalive.csv', 'a') as f_object:
					# Pass this file object to csv.writer()
					# and get a writer object
					writer_object = writer(f_object)

					# Pass the list as an argument into
					# the writerow()
					writer_object.writerow(candleData)

					# Close the file object
					f_object.close()

				lastbar = bar

		else:
			lastbar = bar

	def error(self, reqId, errorCode, errorString):
		print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)

	def startData(self, ticker):
		print("testy")
		queryTime = (datetime.datetime.today() - datetime.timedelta(days=0)).strftime("%Y%m%d %H:%M:%S")

		contract = Contract()
		contract.secType = "STK"
		contract.symbol = ticker
		contract.currency = "USD"
		contract.exchange = "SMART"

		self.reqHistoricalData(1, contract, "", "1 D", "1 min", "TRADES", 1, 1, True, [])

		f = open("./MainStoinker/DataCollection/Test/datalive.csv", "w")
		f.truncate()
		f.close()

		self.df = pd.DataFrame()
		self.dataArray = np.zeros(0)
		self.test = 13