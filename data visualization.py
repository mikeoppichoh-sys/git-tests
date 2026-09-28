import matplotlib.pyplot as plt
squares=[1,4,9,16,25]
plt.style.use('seaborn-v0_8-darkgrid')
fig, ax = plt.subplots()
ax.plot(range(1, 6), squares, linewidth=3)
ax.set_title("Square Numbers", fontsize=24)
ax.set_xlabel("Value", fontsize=14)
ax.set_ylabel("Square of Value", fontsize=14)
ax.tick_params(labelsize=14)
plt.show()