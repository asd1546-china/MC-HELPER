import launch_mc
import testing_gpu_monitor
MC_HELPER = 1
def mian():
    print("欢迎使用python craft launcher")
    print("模式列表".center(20, '=') + "：")
    print("1.启动mine craft ")
    print("2.更改实例 ")
    print("3.启动性能仪表盘")
    print("4.模组转移")
    print("5.退出")
    choos=input("请输入代码： ")
    if choos=="1":
        launch_mc.main_minecraftlauncher()
    elif choos=="2":
        pass
    elif choos=="3":
        testing_gpu_monitor.main_monitor()
    elif choos == "4":
        pass
    elif choos=="5":
        pass

while MC_HELPER:
    mian()
