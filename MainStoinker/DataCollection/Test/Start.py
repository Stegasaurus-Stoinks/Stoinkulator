import Start_config
import livedatacollect2

from ibapi.client import EClient

app = EClient(livedatacollect2.MyWrapper())
app.connect("127.0.0.1", 7497, clientId=1)
lastbar = 0
app.run()

app.startData(ticker)

