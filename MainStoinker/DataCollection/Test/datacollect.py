from ib_insync import *
from zoneinfo import ZoneInfo
import talib as ta
import yfinance as yf

# util.startLoop()  # uncomment this line when in a notebook

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=1)

contract = Stock('AAPL','SMART','USD')
bars = ib.reqHistoricalData(
    contract, endDateTime='', durationStr='10 D',
    barSizeSetting='1 min', whatToShow='MIDPOINT', useRTH=True, keepUpToDate=True)

#print(bars)
# convert to pandas dataframe:
df = util.df(bars)
df['MA20'] = ta.SMA(df['close'],timeperiod=5)
df['MA50'] = ta.SMA(df['close'],timeperiod=50)
df['LineTest'] = ""
df['LineTest'][1000] = 130
df['LineTest'][2000] = 160

print(df)
print(df['LineTest'][2000])

f = open('./MainStoinker/DataCollection/Test/data.csv','w+')
df.to_csv(f,index=False, header=False,lineterminator='\n')
f.close()
