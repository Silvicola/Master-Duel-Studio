using System.Collections.ObjectModel;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using MDStudio.Models;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;

namespace MDStudio.Pages;

/// <summary>历史页：以卡为单位的三类改动与复原留痕，纯记录、不改动游戏。</summary>
public sealed partial class HistoryPage : Page
{
    private readonly ObservableCollection<HistoryItem> _shown = new();
    private readonly List<HistoryItem> _all = new();

    public HistoryPage()
    {
        InitializeComponent();
        HistoryList.ItemsSource = _shown;
        Loaded += (_, _) => RefreshChips();
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
            _all.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("records", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var row in arr.EnumerateArray())
                {
                    var item = Parse(row);
                    if (item is not null) _all.Add(item);
                }
            }
            ApplyFilter();
        }
        catch (Exception ex)
        {
            SetStatus("读取失败：" + ex.Message);
        }
    }

    private static HistoryItem? Parse(JsonElement r)
    {
        string op = Text(r, "op");
        // 日期进分组头，行内只留时分。解析失败就退回原文，不至于让这条记录消失
        string timeText = Text(r, "time");
        var when = DateTime.TryParseExact(timeText, "yyyy-MM-dd HH:mm",
            CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed)
            ? parsed : DateTime.MinValue;
        string clock = when == DateTime.MinValue ? timeText : when.ToString("HH:mm");
        var (category, filter, opText) = Classify(op);
        if (category.Length == 0) return null;   // 三类与复原之外的记录，这页不显示

        var card = r.TryGetProperty("card", out var c) && c.ValueKind == JsonValueKind.Number
            ? c.GetInt32() : 0;
        var name = Text(r, "name");
        bool isUndo = op == "undo";
        string method = MethodOf(op);

        var det = r.TryGetProperty("detail", out var dv) && dv.ValueKind == JsonValueKind.Object
            ? dv : default;
        int files = det.ValueKind == JsonValueKind.Object &&
                    int.TryParse(Text(det, "files"), out var f) ? f : 0;
        string source = det.ValueKind == JsonValueKind.Object
            ? (Text(det, "source") is { Length: > 0 } s ? s : Text(det, "path"))
            : "";
        string fromText = Text(r, "from");

        if (isUndo)
        {
            // 复原条目跟被撤销的类别走：徽章「复原」+ 类别名，筛选也跟着那一类
            (category, filter, opText) = Text(det, "kind") switch
            {
                "cutin" => ("复原", "cutin", "复原召唤动画"),
                "runfx" => ("复原", "runfx", "复原卡牌特效"),
                "art" => ("复原", "art", "复原超框卡图"),
                _ => ("复原", "", "复原改动"),
            };
        }

        var right1 = new List<string>();
        if (!isUndo && fromText.Length > 0) right1.Add(fromText);
        string right2 = files > 0 ? $"{(isUndo ? "还原" : "改动")} {files} 个文件" : "";

        bool archived = r.TryGetProperty("archived", out var av) && av.ValueKind == JsonValueKind.True;

        var tips = new List<string>();
        if (source.Length > 0) tips.Add(source);
        if (isUndo) tips.Add("复原留痕：改动的文件已还原官方版，这条本身不可撤销");
        else if (archived) tips.Add("这条改动已被复原，保留作记录");
        else if (r.TryGetProperty("undoable", out var u) && u.ValueKind == JsonValueKind.False)
            tips.Add("这条不改游戏文件，没有可复原的内容");

        return new HistoryItem
        {
            Time = timeText,
            Clock = clock,
            When = when,
            Category = category,
            Filter = filter,
            Method = method,
            IsUndo = isUndo,
            OpText = opText,
            CardText = card > 0 ? (name.Length > 0 ? $"{card} {name}" : card.ToString()) : "—",
            Detail1 = string.Join(" · ", right1),
            Detail2 = right2,
            ReadOnly = !isUndo && !archived &&
                       r.TryGetProperty("undoable", out var ua) && ua.ValueKind == JsonValueKind.False,
            Tip = tips.Count > 0 ? string.Join("\n", tips) : null,
        };
    }

    /// <summary>op → (类别徽章, 筛选键, 操作名)。空类别 = 这页不显示。</summary>
    private static (string Category, string Filter, string OpText) Classify(string op) => op switch
    {
        "cutin.video" or "cutin.reskin" or "cutin.borrow" or "cutin.clone"
            or "pack.cutin" => ("动画", "cutin", "生成召唤动画"),
        "cutin.spine" => ("动画", "cutin", "Spine 导入"),
        "runfx.video" or "runfx.borrow" or "pack.runfx" => ("卡牌特效", "runfx", "生成发动特效"),
        "overframe.set" or "pack.art" => ("超框卡图", "art", "修改超框卡图"),
        "undo" => ("复原", "", ""),   // 类别与筛选键由记录里的 kind 决定，见 Parse
        _ => ("", "", ""),
    };

    /// <summary>召唤动画的导入方式徽章。整合包装入记录不了当初的做法，不标。</summary>
    private static string MethodOf(string op) => op switch
    {
        "cutin.video" or "cutin.reskin" or "cutin.borrow" or "cutin.clone" => "视频",
        "cutin.spine" => "Spine",
        _ => "",
    };

    // ----------------------------------------------------------------- 筛选
    private string _filter = "all";

    private void Filter_Changed(object sender, RoutedEventArgs e)
    {
        if (sender is Button b && b.Tag is string t) _filter = t;
        RefreshChips();
        ApplyFilter();
    }

    /// <summary>选中胶囊浅底描边深字，未选白底灰字。颜色随当前主题取。</summary>
    private void RefreshChips()
    {
        foreach (var chip in new[] { ChipAll, ChipArt, ChipCutin, ChipRunfx })
        {
            var on = (chip.Tag as string) == _filter;
            chip.Background = Pick(on ? "ChipCheckedBgBrush" : "CardBackgroundFillColorDefaultBrush");
            chip.BorderBrush = Pick(on ? "ChipCheckedBorderBrush" : "CardStrokeColorDefaultBrush");
            chip.Foreground = Pick(on ? "ChipCheckedTextBrush" : "TextFillColorSecondaryBrush");
        }
    }

    private static Brush Pick(string key)
    {
        var res = Application.Current.Resources;
        bool dark = (App.MainWindow?.Content as FrameworkElement)?.ActualTheme == ElementTheme.Dark;
        if (res.ThemeDictionaries.TryGetValue(dark ? "Dark" : "Light", out var dict)
            && dict is ResourceDictionary rd
            && rd.TryGetValue(key, out var v) && v is Brush b)
            return b;
        if (res.TryGetValue(key, out var any) && any is Brush b2) return b2;
        return new SolidColorBrush(Microsoft.UI.Colors.Gray);
    }

    /// <summary>这一页只看最近三天（今天、昨天、前天）。更早的记录还在 journal 里，
    /// 只是不进列表，页脚有一句说明。</summary>
    private const int WindowDays = 3;

    private void ApplyFilter()
    {
        var tag = _filter;
        var since = DateTime.Today.AddDays(-(WindowDays - 1));
        var recent = _all.Where(x => x.When >= since).ToList();
        // 复原条目跟被撤销的类别走：筛选「动画」时也能看到那次复原
        var rows = tag == "all" ? recent : recent.Where(x => x.Filter == tag).ToList();

        _shown.Clear();
        // 按天分组：日期一变就插一条分隔行。组头本身不是记录，不计入条数
        foreach (var g in rows.GroupBy(x => x.When.Date))
        {
            _shown.Add(HistoryItem.MakeHeader($"{DayLabel(g.Key)} · {g.Count()} 条"));
            foreach (var item in g) _shown.Add(item);
        }

        CountText.Text = tag == "all"
            ? $"近三天 {rows.Count} 条"
            : $"近三天 {rows.Count} / {recent.Count} 条";
        // 三种空：从没做过、三天内没有、这一类三天内没有。混成一句会误导
        EmptyHint.Text = _all.Count == 0 ? "还没有改动记录"
            : recent.Count == 0 ? "最近三天没有改动"
            : "这一类最近三天没有记录";
        EmptyHint.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        HistoryList.Visibility = rows.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
    }

    private static string DayLabel(DateTime day)
    {
        var today = DateTime.Today;
        if (day == today) return "今天";
        if (day == today.AddDays(-1)) return "昨天";
        return $"{day.Month}月{day.Day}日";
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e) => await LoadAsync();

    // ----------------------------------------------------------------- 日志
    /// <summary>复制本次运行日志到剪贴板。日志同时实时落盘，这里把路径告诉用户。</summary>
    private void CopyLog_Click(object sender, RoutedEventArgs e)
    {
        var text = App.LogSnapshot();
        if (text.Length == 0)
        {
            SetStatus("日志是空的，没有可复制的内容");
            return;
        }
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(text);
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
        var path = App.LogFilePath;
        SetStatus(path.Length > 0
            ? $"已复制日志，并保存到 {path}"
            : "已复制日志");
    }

    private void SetStatus(string text)
    {
        StatusText.Text = text;
        StatusText.Visibility = Visibility.Visible;
    }

    // ----------------------------------------------------------------- 对话框
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
}
