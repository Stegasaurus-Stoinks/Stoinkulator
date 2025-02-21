import pandas as pd

df = pd.read_csv('algo.csv')

# print(df.to_string()) 
filtered_df = df[df['profit'] > 0]
# print(filtered_df)

print("total trades: " , df.shape[0])
print("winning trades: ", filtered_df.shape[0])
print("Percent: ", filtered_df.shape[0]/df.shape[0], "%")

