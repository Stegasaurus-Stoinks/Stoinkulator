from trade import Trade

class testibkr():
    def __init__(self, api):
        self.api = api
        print("Read positions in external class:")
        print(self.api.readPositions())
        self.testpositions(self.api)


    def testpositions(self, api):
        self.api2 = api
        print("Read positions in external class function:")
        print(self.api2.readPositions())

        trade = Trade("AAPL", 10, 5, 3.00, 5555, 5, False, 0.20, self.api2, printInfo=False)

