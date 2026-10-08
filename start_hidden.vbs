Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = "E:\daily_stock_analysis-main"
ws.Run "cmd /c start_sea.bat", 0, False
