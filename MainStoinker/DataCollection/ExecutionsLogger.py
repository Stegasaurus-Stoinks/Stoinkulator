from MainStoinker.NeatTools.decorators import singleton
import MainStoinker.MainStuff.Start_config as config
from ibapi import execution as ibexecution
from ibapi import contract as ibcontract

# execution data : self.execId, self.time, self.acctNumber, 
#                 self.exchange, self.side, self.shares, self.price, self.permId, self.clientId, self.orderId, self.liquidation,
#                 self.cumQty, self.avgPrice, self.orderRef, self.evRule, self.evMultiplier, self.modelCode, self.lastLiquidity

# contract data : str(self.conId),str(self.ratio),str(self.action),str(self.exchange),str(self.openClose),str(self.shortSaleSlot),
                # str(self.designatedLocation),str(self.exemptCode)))

# [[OrderId, PermId, Execution, Contract, CompletedOrder],[OrderId, PermId, Execution, Contract, CompletedOrder]]

#in file debug variable because once this works we shouldnt need to mess with it and we can just shut the prints off :D
debug = True

@singleton
class ExecutionLog:
    
    def __init__(self):
        self.executionArray = []
        
    def add_execution(self, execution:ibexecution, contract:ibcontract):
        
        if debug == True:
            generalDataString = "OrderId : "+str(execution.orderId)+" PermId : "+str(execution.permId)+" | "+str(execution.side)+" "+str(execution.shares) + " shares of " + str(contract.symbol) 
            priceDataString = "Execution Price : " + str(execution.price) + " Execution Time : " + str(execution.time)
            print(generalDataString)
            print(priceDataString)
            
        for entry in self.executionArray:
            if entry[1] == execution.permId:
                if debug == True:
                    print("Found matching PermId : Modifying Existing Execution")
                    entry[0] = execution.orderId    
                    entry[2] = execution
                    entry[3] = contract
                    
                    return
                
        newEntry = [-1] * 5
        newEntry[0] = execution.orderId
        newEntry[1] = execution.permId
        newEntry[2] = execution
        newEntry[3] = contract
        
        self.executionArray.append(newEntry)
        
    def add_completed_order(self,permId,orderState):
        if debug == True:
            print("recieved Completed Order CallBack, checking order list for matching PermId")
            
        for entry in self.executionArray:
            if permId == entry[1]:
                if debug == True:
                    print("Found matching PermId : Modifying Existing Execution")
                    entry[4] = orderState
                    return
                
        # If there is no existing entry with matching permId, this must be new data: 
        newEntry = []
        newEntry[1] = permId
        newEntry[4] = orderState
        
        self.executionArray.append(newEntry)
        return
    
    def return_execution_details(self,OrderId):
        for entry in self.executionArray:
            if entry[0] == OrderId:
                return entry
            
            else:
                return 0
            
    def remove_execution_details(self,OrderId):
        #IDK if this will get used?  would clear the array size a bit which might be good?  but like the completed orders call back gives all the completed orders all the time so like who even knows at this point
        for i in range(0, len(self.executionArray), 1):
            if self.executionArray[i][0] == OrderId:
                del self.executionArray[i]
                return
            
    def __str__(self):
        temp = "testy \n test2 \n test3" + " \n Length of Array " + str(len(self.executionArray))
        
        return temp
        return "this is the super cool custom print function I want to figure out how to do"
                
        
    
                
        