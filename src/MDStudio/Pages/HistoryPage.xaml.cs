using System.Collections.ObjectModel;
using System.Text.Json;
using MDStudio.Models;
using MDStudio.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;

namespace MDStudio.Pages;

/// <summary>历史页：这个工具做过的所有改动，可以逐条撤销。</summary>
public sealed partial class HistoryPage : Page
{
    private readonly ObservableCollection<HistoryItem> _records = new();

    public HistoryPage()
    {
        InitializeComponent();
        HistoryList.ItemsSource = _records;
        LogList.ItemsSource = App.Log;

        HistoryList.SelectionChanged += (_, _) => UpdateButtons();

        if (App.Engine is { } engine)
        {
            engine.LogReceived += OnEngineLog;
            engine.ProgressChanged += OnEngineProgress;
        }
        Unloaded += OnUnloaded;
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        if (App.Engine is { } engine)
        {
            engine.LogReceived -= OnEngineLog;
            engine.ProgressChanged -= OnEngineProgress;
        }
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        if (App.Engine is null)
        {
            await InfoAsync("引擎没有启动", App.EngineError ?? "未知原因");
            return;
        }
        await LoadAsync();
    }

    // ----------------------------------------------------------------- 读记录
    private async Task LoadAsync()
    {
        if (App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("history.list");
            _records.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("records", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var r in arr.EnumerateArray())
                {
                    var card = r.TryGetProperty("card", out var c) && c.ValueKind == JsonValueKind.Number
                        ? c.GetInt32() : 0;
                    var name = Text(r, "name");
                    // 整合包的导出/安装记录没有单张卡, 用卡数代替
                    var cards = r.TryGetProperty("cards", out var cv) && cv.ValueKind == JsonValueKind.Number
                        ? cv.GetInt32() : -1;
                    var total = r.TryGetProperty("total", out var tv) && tv.ValueKind == JsonValueKind.Number
                        ? tv.GetInt32() : -1;
                    string countText;
                    if (card > 0)
                        countText = name.Length > 0 ? $"{card} {name}" : card.ToString();
                    else if (cards >= 0)
                        countText = total > 0 ? $"{cards} / {total} 张卡" : $"{cards} 张卡";
                    else
                        countText = "—";
                    // 壁纸的 op 都是 wallpaper.set，用 meta 里的 label（大厅壁纸 / 大厅背景）区分，
                    // 否则历史里会一律显示成「替换大厅壁纸」。
                    string op = Text(r, "op");
                    string fromText = Text(r, "from");
                    bool isWallpaper = op.StartsWith("wallpaper.");
                    _records.Add(new HistoryItem
                    {
                        Id = Text(r, "id"),
                        OpText = isWallpaper ? WallpaperText(fromText) : OpText(op),
                        CardText = countText,
                        Time = Text(r, "time"),
                        From = isWallpaper ? "" : fromText,
                        Undoable = !r.TryGetProperty("undoable", out var u) ||
                                   u.ValueKind != JsonValueKind.False,
                    });
                }
            }
            SetStatus($"共 {_records.Count} 条记录");
            UpdateButtons();
        }
        catch (Exception ex)
        {
            SetStatus("读取失败：" + ex.Message);
        }
    }

    /// <summary>取一个可能是字符串也可能是数字的字段。后端的 meta 里两种都有。</summary>
    private static string Text(JsonElement e, string name)
    {
        if (!e.TryGetProperty(name, out var v)) return "";
        return v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.ToString(),
            JsonValueKind.True => "是",
            JsonValueKind.False => "否",
            _ => "",
        };
    }

    private static string OpText(string op) => op switch
    {
        "cutin.borrow" => "借用召唤动画",
        "cutin.clone" => "克隆召唤动画",
        "cutin.video" => "视频召唤动画",
        "cutin.reskin" => "换皮",
        "cutin.spine" => "Spine 导入",
        "pack.cutin" => "整合包·召唤动画",
        "runfx.borrow" => "借用发动特效",
        "runfx.video" => "视频发动特效",
        "pack.runfx" => "整合包·发动特效",
        "pack.art" => "整合包·卡图",
        "pack.export" => "导出整合包",
        "pack.install" => "安装整合包",
        "wallpaper.set" => "替换大厅壁纸",
        _ => op,
    };

    /// <summary>壁纸记录按 meta 里的 label 显示：大厅壁纸 / 大厅背景 / 两者都换。</summary>
    private static string WallpaperText(string label) => label switch
    {
        var s when s.Contains("大厅背景") && s.Contains("大厅壁纸") => "替换大厅壁纸/背景",
        var s when s.Contains("大厅背景") => "替换大厅背景",
        _ => "替换大厅壁纸",
    };

    private void UpdateButtons()
    {
        var sel = HistoryList.SelectedItem as HistoryItem;
        UndoSelectedBtn.IsEnabled = sel is { Undoable: true };
        UndoLastBtn.IsEnabled = _records.Any(r => r.Undoable);
    }

    // ------------------------------------------------------------------- 撤销
    private async void UndoSelected_Click(object sender, RoutedEventArgs e)
    {
        if (HistoryList.SelectedItem is not HistoryItem item) return;
        if (!item.Undoable)
        {
            await InfoAsync("这条不能撤销", "导出整合包不会改动游戏文件，所以没有可撤销的内容。");
            return;
        }
        await UndoAsync(item, "确定撤销这条吗？");
    }

    private async void UndoLast_Click(object sender, RoutedEventArgs e)
    {
        // 跳过不可撤销的记录 (导出整合包), 撤的是最近一条真正改了游戏的
        var last = _records.FirstOrDefault(r => r.Undoable);
        if (last is null) return;
        await UndoAsync(last, "确定撤销最近一条吗？");
    }

    private async Task UndoAsync(HistoryItem item, string question)
    {
        if (App.DryRun)
        {
            await InfoAsync("预演模式", "预演模式下不执行撤销。");
            return;
        }
        if (!await ConfirmAsync("撤销", $"{question}\n{item.Time}　{item.OpText}　{item.CardText}"))
            return;

        var args = new Dictionary<string, object?> { ["ids"] = new[] { item.Id } };
        var data = await RunJobAsync("history.undo", args, "正在撤销…", "撤销完成");
        if (data.ValueKind == JsonValueKind.Object &&
            data.TryGetProperty("undone", out var arr) && arr.ValueKind == JsonValueKind.Array &&
            arr.GetArrayLength() > 0)
        {
            var n = arr.GetArrayLength();
            SetStatus(n > 1 ? $"撤销完成，实际撤掉 {n} 条（含建在它上面的改动）" : "撤销完成");
        }
        await LoadAsync();
    }

    // --------------------------------------------------------------- 跑动作
    private async Task<JsonElement> RunJobAsync(string op, Dictionary<string, object?> args,
        string running, string done)
    {
        if (App.Engine is not { } engine) return default;

        SetBusy(true);
        SetStatus(running);
        JsonElement data = default;
        try
        {
            data = await engine.CallAsync(op, args);
            SetStatus(done);
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("失败：" + ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
        return data;
    }

    private void SetStatus(string text)
    {
        StatusStrip.Visibility = Visibility.Visible;
        StatusText.Text = text;
    }

    /// <summary>把整份日志拷到剪贴板，方便贴给别人看（列表里也能直接选中一小段）。</summary>
    private void CopyLog_Click(object sender, RoutedEventArgs e)
    {
        if (App.Log.Count == 0)
        {
            SetStatus("日志是空的，没有可复制的内容");
            return;
        }
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(string.Join("\r\n", App.Log));
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
        SetStatus($"已复制 {App.Log.Count} 行日志");
    }

    private void SetBusy(bool busy)
    {
        UndoSelectedBtn.IsEnabled = !busy && HistoryList.SelectedItem is HistoryItem { Undoable: true };
        UndoLastBtn.IsEnabled = !busy && _records.Any(r => r.Undoable);
        HistoryList.IsEnabled = !busy;
        Progress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        Progress.IsIndeterminate = busy;
        if (!busy)
        {
            Progress.Value = 0;
            Progress.Maximum = 100;
        }
    }

    // --------------------------------------------------------------- 引擎事件
    private void OnEngineLog(string level, string text)
        => DispatcherQueue.TryEnqueue(() =>
        {
            App.AddLog(level, text);
            if (!LogExpander.IsExpanded && level is "WARNING" or "ERROR")
                LogExpander.IsExpanded = true;
            if (App.Log.Count > 0) LogList.ScrollIntoView(App.Log[^1]);
        });

    private void OnEngineProgress(string text, int i, int n)
        => DispatcherQueue.TryEnqueue(() =>
        {
            StatusStrip.Visibility = Visibility.Visible;
            Progress.Visibility = Visibility.Visible;
            StatusText.Text = text;
            Progress.IsIndeterminate = i < 0 || n <= 0;
            if (i >= 0 && n > 0)
            {
                Progress.IsIndeterminate = false;
                Progress.Maximum = n;
                Progress.Value = i;
            }
        });

    // --------------------------------------------------------------- 对话框
    private async Task<bool> ConfirmAsync(string title, string body)
    {
        var dlg = new ContentDialog
        {
            Title = title,
            Content = body,
            PrimaryButtonText = "继续",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = XamlRoot,
        };
        return await dlg.ShowAsync() == ContentDialogResult.Primary;
    }

    private async Task InfoAsync(string title, string body)
    {
        var dlg = new ContentDialog
        {
            Title = title,
            Content = body,
            CloseButtonText = "好",
            XamlRoot = XamlRoot,
        };
        await dlg.ShowAsync();
    }
}
