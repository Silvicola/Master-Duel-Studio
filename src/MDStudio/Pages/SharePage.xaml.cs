using System.Collections.ObjectModel;
using System.Text.Json;
using MDStudio.Models;
using MDStudio.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using Windows.Storage.Pickers;

namespace MDStudio.Pages;

/// <summary>
/// 整合包页：导出你做过的卡（按每张卡的三类内容分别勾）、导入别人的包（先预览再选装）、
/// 以及把装过的包整包复原。
///
/// 预览和安装走同一个界面：「预览并安装」打开预览，取消就是只看，勾好确认才装。
/// 判断逻辑（哪张卡能装、默认勾哪些）只在预览里做一次，后端按勾选清单执行。
/// </summary>
public sealed partial class SharePage : Page
{
    private readonly ObservableCollection<PackCardItem> _exportable = new();
    private readonly ObservableCollection<PackPreviewItem> _preview = new();
    private readonly ObservableCollection<PackInstalledItem> _installed = new();

    /// <summary>全部可导出卡。搜索时按它过滤出 _exportable，勾选状态留在同一个对象上。</summary>
    private readonly List<PackCardItem> _allExportable = new();
    // 自己有任务在跑才响应引擎进度广播（动画页同款守卫）。本页进页订阅出页退订，
    // 但用户停在本页时，别页任务（如设置页下载组件）的进度也会广播进来。
    private bool _jobActive;

    public SharePage()
    {
        InitializeComponent();
        ExportList.ItemsSource = _exportable;
        InstalledList.ItemsSource = _installed;

        // 日志由 App 单点订阅（见 App.OnLaunched），这里只留进度订阅。
        if (App.Engine is { } engine)
            engine.ProgressChanged += OnEngineProgress;
        Unloaded += OnUnloaded;
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        if (App.Engine is { } engine)
            engine.ProgressChanged -= OnEngineProgress;
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        if (App.Engine is null)
        {
            await Dialogs.InfoAsync(XamlRoot, "引擎没有启动", App.EngineError ?? "未知原因", danger: true);
            return;
        }
        await LoadCardsAsync();
        await LoadInstalledAsync();
    }

    // ------------------------------------------------------------- 可导出的卡
    private async Task LoadCardsAsync()
    {
        if (App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("pack.list");
            _allExportable.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("cards", out var cards) &&
                cards.ValueKind == JsonValueKind.Object)
            {
                foreach (var p in cards.EnumerateObject())
                {
                    if (!int.TryParse(p.Name, out var id)) continue;
                    var kinds = p.Value.TryGetProperty("kinds", out var k) &&
                                k.ValueKind == JsonValueKind.Array
                        ? k.EnumerateArray().Select(x => x.GetString() ?? "").ToHashSet()
                        : new HashSet<string>();
                    var name = p.Value.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                    _allExportable.Add(new PackCardItem
                    {
                        Id = id,
                        Name = name,
                        HasCutin = kinds.Contains("cutin"),
                        HasRunfx = kinds.Contains("runfx"),
                        HasArt = kinds.Contains("art"),
                    });
                }
            }
            ApplyExportFilter();
            ExportHint.Text = _allExportable.Count > 0
                ? $"可导出 {_allExportable.Count} 张卡。一张卡至少勾一类。"
                : "";
        }
        catch (Exception ex)
        {
            SetStatus("读取失败：" + ex.Message);
        }
    }

    /// <summary>按搜索词过滤。用的是同一批对象，所以切回全部时勾选状态还在。</summary>
    private void ApplyExportFilter()
    {
        var q = ExportSearchBox.Text.Trim();
        _exportable.Clear();
        foreach (var c in _allExportable)
        {
            if (q.Length > 0 &&
                c.Name.IndexOf(q, StringComparison.OrdinalIgnoreCase) < 0 &&
                !c.Id.ToString().Contains(q))
                continue;
            _exportable.Add(c);
        }
        var any = _allExportable.Count > 0;
        ExportEmpty.Visibility = any ? Visibility.Collapsed : Visibility.Visible;
        ExportList.Visibility = any ? Visibility.Visible : Visibility.Collapsed;
        if (any && _exportable.Count == 0)
            ExportHint.Text = "没有匹配的卡。";
        else if (any)
            ExportHint.Text = $"可导出 {_allExportable.Count} 张卡。一张卡至少勾一类。";
    }

    private void ExportSearchBox_TextChanged(object sender, TextChangedEventArgs e)
        => ApplyExportFilter();

    // ------------------------------------------------------------------- 导出
    private async void ExportBtn_Click(object sender, RoutedEventArgs e)
    {
        // 收集每张卡勾了什么。一张卡一类都没勾就不进包。
        var cards = new List<int>();
        var kinds = new Dictionary<int, string[]>();
        var nCutin = 0;
        var nRunfx = 0;
        var nArt = 0;
        foreach (var c in _allExportable)
        {
            var ks = new List<string>();
            if (c.HasCutin && c.Cutin) { ks.Add("cutin"); nCutin++; }
            if (c.HasRunfx && c.Runfx) { ks.Add("runfx"); nRunfx++; }
            if (c.HasArt && c.Art) { ks.Add("art"); nArt++; }
            if (ks.Count == 0) continue;
            cards.Add(c.Id);
            kinds[c.Id] = ks.ToArray();
        }
        if (cards.Count == 0)
        {
            await Dialogs.InfoAsync(XamlRoot, "没有要导出的内容",
                "至少勾选一张卡里的一类内容。");
            return;
        }

        var folder = await PickFolderAsync();
        if (folder is null) return;

        var name = PackNameBox.Text.Trim();
        if (name.Length == 0) name = AutoPackName(nCutin, nRunfx, nArt);

        var args = new Dictionary<string, object?>
        {
            ["cards"] = cards,
            ["kinds"] = kinds,
            ["out_path"] = folder,
            ["name"] = name,
            ["zip_it"] = true,
        };

        var data = await RunJobAsync("pack.export", args,
            "正在压缩…", "导出完成");
        if (data.ValueKind == JsonValueKind.Object &&
            data.TryGetProperty("path", out var p) && p.GetString() is { } path)
        {
            SetStatus("已导出到 " + path);
            var skipped = DescribeSkipped(data);
            await Dialogs.InfoAsync(XamlRoot, "导出完成",
                $"{cards.Count} 张卡，{nCutin + nRunfx + nArt} 条内容。",
                danger: false, cardName: null, cardMeta: null,
                note: skipped.Length > 0 ? $"{skipped}\n{path}" : path);
        }
    }

    /// <summary>把后端跳过的条目翻成一句人话。没有跳过就是空串。</summary>
    private static string DescribeSkipped(JsonElement data)
    {
        if (data.ValueKind != JsonValueKind.Object ||
            !data.TryGetProperty("skipped", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return "";
        int fusion = 0, missing = 0;
        foreach (var item in arr.EnumerateArray())
        {
            string why = item.TryGetProperty("why", out var w) ? w.GetString() ?? "" : "";
            if (why == "fusion") fusion++;
            else missing++;
        }
        var parts = new List<string>();
        if (fusion > 0) parts.Add($"{fusion} 张是融合召唤演出，改了不会播，已跳过");
        if (missing > 0) parts.Add($"{missing} 张的依赖文件已不在盘上，先复原或重新生成");
        return parts.Count > 0 ? "另有 " + string.Join("；", parts) + "。" : "";
    }

    /// <summary>没填名字时的默认名：导出(动画+2、特效+1)_20261111_154712。</summary>
    private static string AutoPackName(int cutin, int runfx, int art)
    {
        var parts = new List<string>();
        if (cutin > 0) parts.Add($"动画+{cutin}");
        if (runfx > 0) parts.Add($"特效+{runfx}");
        if (art > 0) parts.Add($"超框+{art}");
        return $"导出({string.Join("、", parts)})_{DateTime.Now:yyyyMMdd_HHmmss}";
    }

    // ------------------------------------------------------------------- 导入
    private async void PackBrowseBtn_Click(object sender, RoutedEventArgs e)
    {
        if (await PickPackFileAsync() is not { } path) return;
        // 选完先验一次：不是 zip、不是本工具的包，当场说清楚，别等点安装才报
        if (!await ImportCheck.EnsureAsync(XamlRoot, path, "pack")) return;
        PackPathBox.Text = path;
    }

    private async void InstallBtn_Click(object sender, RoutedEventArgs e)
        => await OpenPreviewAsync();

    /// <summary>
    /// 读写整合包之前的闸门。返回 false 表示不该继续，原因已经告诉用户了。
    ///
    /// 只拦三件必然出事的事：找不到游戏目录、账号没定（或选定账号的目录暂时不在）、
    /// 游戏还开着。卡索引重建、超框登记表定位只是慢，不拦：拦了也只能干等，
    /// 那两项没有可信进度可看。
    /// </summary>
    private async Task<bool> PassInstallGateAsync()
    {
        if (App.Engine is not { } engine) return false;
        JsonElement r;
        try
        {
            r = await engine.CallAsync("app.ready",
                new Dictionary<string, object?> { ["check_game"] = true });
        }
        catch (Exception ex)
        {
            await Dialogs.InfoAsync(XamlRoot, "检查不了当前状态", ex.Message, danger: true);
            return false;
        }

        if (!Flag(r, "game_dir_ok"))
        {
            await Dialogs.InfoAsync(XamlRoot, "找不到游戏目录",
                "软件要靠游戏文件才能读包和安装。请到设置里指定 Master Duel 的安装位置。",
                danger: true);
            return false;
        }
        if (!Flag(r, "account_ok"))
        {
            var missing = Flag(r, "account_missing");
            var go = await Dialogs.ConfirmAsync(XamlRoot,
                missing ? "选定的账号暂时不可用" : "还没有选择账号",
                missing
                    ? "那个账号目录现在找不到，游戏可能正在更新。安装会把备份和记录写进账号目录，现在装会写到别处。"
                    : "一个游戏目录下可能有多个账号，各自是一份独立的游戏文件。安装要把备份和记录写到选定账号的目录下。",
                "去设置", null, null,
                "账号没定就安装，复原和备份都会对不上。");
            if (go) (App.MainWindow as MainWindow)?.GoToSettings();
            return false;
        }
        if (Flag(r, "game_running"))
        {
            await Dialogs.InfoAsync(XamlRoot, "游戏还开着",
                "安装要写入游戏文件，请先完全关闭游戏（包括后台进程）再回来。",
                danger: true);
            return false;
        }
        return true;
    }

    private static bool Flag(JsonElement r, string key)
        => r.ValueKind == JsonValueKind.Object && r.TryGetProperty(key, out var v)
           && v.ValueKind == JsonValueKind.True;

    /// <summary>
    /// 打开预览。取消就是只看，勾好确认才装。
    /// </summary>
    private async Task OpenPreviewAsync()
    {
        if (App.Engine is not { } engine) return;
        var path = PackPathBox.Text;
        if (string.IsNullOrWhiteSpace(path))
        {
            await Dialogs.InfoAsync(XamlRoot, "还没选整合包", "先选一个压缩包。");
            return;
        }
        if (!await PassInstallGateAsync()) return;

        SetBusy(true);
        SetStatus("正在读取整合包…");
        JsonElement data;
        try
        {
            data = await engine.CallAsync("pack.inspect",
                new Dictionary<string, object?> { ["pack_path"] = path });
        }
        catch (Exception ex)
        {
            SetBusy(false);
            ClearStatus();
            await Dialogs.InfoAsync(XamlRoot, "读不了这个整合包", ex.Message, danger: true);
            return;
        }
        SetBusy(false);
        // 读完了就把状态条收起：预览对话框打开期间底部不该挂着"正在读取"。
        // 取消安装、读完直接关弹窗都会 return，不复位就会一直留着（用户实测）。
        ClearStatus();

        _preview.Clear();
        var packName = data.TryGetProperty("name", out var pn) ? pn.GetString() ?? "" : "";
        var tool = data.TryGetProperty("tool", out var tl) ? tl.GetString() ?? "" : "";
        var size = data.TryGetProperty("size", out var sz) ? sz.GetInt64() : 0;

        if (data.TryGetProperty("cards", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var c in arr.EnumerateArray())
            {
                var has = c.TryGetProperty("has", out var h) ? h : default;
                var st = c.TryGetProperty("state", out var s) ? s : default;
                var bl = c.TryGetProperty("blocked", out var b) ? b : default;

                string StateOf(string kind) =>
                    st.ValueKind == JsonValueKind.Object &&
                    st.TryGetProperty(kind, out var v) && v.ValueKind == JsonValueKind.String
                        ? StateText(v.GetString() ?? "") : "无";

                string BlockOf(string kind) =>
                    bl.ValueKind == JsonValueKind.Object &&
                    bl.TryGetProperty(kind, out var v) && v.ValueKind == JsonValueKind.String
                        ? v.GetString() ?? "" : "";

                bool HasOf(string kind) =>
                    has.ValueKind == JsonValueKind.Object &&
                    has.TryGetProperty(kind, out var v) && v.ValueKind == JsonValueKind.True;

                var id = c.TryGetProperty("mrk", out var m) ? m.GetInt32() : 0;
                var name = c.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";

                // 不能装的显示原因，能装的按状态决定默认勾不勾：干净的直接勾上，
                // 官方和已修改的留着让用户自己点（覆盖别人或官方的要他自己确认）。
                (bool en, string tag, bool chk) Slot(string kind)
                {
                    if (!HasOf(kind)) return (false, "无", false);
                    var why = BlockOf(kind);
                    if (why.Length > 0) return (false, why, false);
                    var st2 = StateOf(kind);
                    return (true, st2, st2 == "无");
                }

                var cutin = Slot("cutin");
                var runfx = Slot("runfx");
                var art = Slot("art");

                // 被拦的小项汇总成一句，整行下方显示。官方/借用/无三种状态不拦。
                var notes = new List<string>();
                if (cutin.tag.StartsWith("本机已改过")) notes.Add("动画到动画页复原");
                if (runfx.tag.StartsWith("本机已改过")) notes.Add("特效到动画页复原");
                if (art.tag.StartsWith("本机已改过")) notes.Add("超框到卡图页复原");
                var blockNote = notes.Count > 0 ? "本机已改过：" + string.Join("，", notes) : "";

                _preview.Add(new PackPreviewItem
                {
                    Id = id,
                    Name = name,
                    HasCutin = HasOf("cutin"),
                    HasRunfx = HasOf("runfx"),
                    HasArt = HasOf("art"),
                    CutinTag = cutin.tag,
                    RunfxTag = runfx.tag,
                    ArtTag = art.tag,
                    CutinEnabled = cutin.en,
                    RunfxEnabled = runfx.en,
                    ArtEnabled = art.en,
                    Cutin = cutin.chk,
                    Runfx = runfx.chk,
                    Art = art.chk,
                    BlockNote = blockNote,
                });
            }
        }

        var picked = await ShowPreviewDialogAsync(packName, tool, size);
        if (picked is null) return;

        if (picked.Count == 0)
        {
            await Dialogs.InfoAsync(XamlRoot, "没有勾选内容",
                "至少勾一张卡里的一类内容。");
            return;
        }

        var args = new Dictionary<string, object?>
        {
            ["pack_path"] = path,
            ["selected"] = picked,
            // 统一覆盖语义（2026-11-11）：勾了就装、装就覆盖，后端不再有跳过
        };
        // 预览可能开着好几分钟，这期间账号可能被切走、游戏可能被启动，装之前再查一次
        if (!await PassInstallGateAsync()) return;
        var installed = await RunJobAsync("pack.install", args, "正在安装…", "安装完成");
        await LoadCardsAsync();
        await LoadInstalledAsync();
        // 覆盖了你自己之前改动的条目数：让用户知道覆盖发生过（旧版本有备份，可在对应页面复原找回）
        var nOver = installed.ValueKind == JsonValueKind.Object &&
                    installed.TryGetProperty("overwritten", out var ov) ? ov.GetInt32() : 0;
        var skipNote = DescribeSkipped(installed);
        if (nOver > 0)
            await Dialogs.InfoAsync(XamlRoot, "安装完成",
                $"有 {nOver} 条覆盖了你之前做过的改动。",
                cardName: null, cardMeta: null,
                note: skipNote.Length > 0
                    ? $"{skipNote}\n被覆盖的版本已自动备份，可在对应页面复原找回。"
                    : "被覆盖的版本已自动备份，可在对应页面复原找回。");
        else if (skipNote.Length > 0)
            await Dialogs.InfoAsync(XamlRoot, "安装完成",
                "部分内容没有装。", cardName: null, cardMeta: null, note: skipNote);
    }

    private static string StateText(string s) => s switch
    {
        "official" => "官方",
        "modified" => "已修改",
        _ => "无",
    };

    private async Task<List<Dictionary<string, object>>?> ShowPreviewDialogAsync(
        string packName, string tool, long size)
    {
        var res = Application.Current.Resources;
        var captionStyle = (Style)res["CaptionTextBlockStyle"];
        var secondary = (Brush)res["TextFillColorSecondaryBrush"];

        var body = new StackPanel { Spacing = 12, MinWidth = 660 };

        var head = new StackPanel { Spacing = 3 };
        head.Children.Add(new TextBlock
        {
            Text = string.IsNullOrWhiteSpace(packName) ? "未命名整合包" : packName,
            FontSize = 20,
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap,
        });
        var meta = new List<string>();
        if (!string.IsNullOrWhiteSpace(tool)) meta.Add(tool);
        meta.Add(_preview.Count > 0 ? $"{_preview.Count} 张卡" : "里面没有卡");
        if (size > 0) meta.Add(FormatSize(size));
        if (_preview.Count > 8) meta.Add("可上下滚动");
        head.Children.Add(new TextBlock
        {
            Text = string.Join(" · ", meta),
            Style = captionStyle,
            Foreground = secondary,
        });
        head.Children.Add(new TextBlock
        {
            Text = "整合包不含音效，装到的卡会用你机器上现有的声音。",
            Style = captionStyle,
            Foreground = secondary,
            TextWrapping = TextWrapping.Wrap,
        });
        body.Children.Add(head);

        var list = new ListView
        {
            ItemsSource = _preview,
            ItemTemplate = (DataTemplate)Resources["PreviewRowTemplate"],
            SelectionMode = ListViewSelectionMode.None,
            MaxHeight = 340,
        };
        ScrollViewer.SetVerticalScrollBarVisibility(list, ScrollBarVisibility.Visible);
        body.Children.Add(new Border
        {
            Padding = new Thickness(12, 8, 12, 8),
            Background = res["LayerFillColorAltBrush"] as Brush,
            BorderBrush = res["PanelBorderBrush"] as Brush,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Child = list,
        });

        // 按钮行：自绘、右对齐，和另外几页的确认框同一套排法。
        // 取消 = 只看，勾好再点「安装选中项」。
        var primary = new Button
        {
            Content = "安装选中项",
            Style = res["AccentButtonStyle"] as Style,
            MinWidth = 96,
        };
        var cancel = new Button
        {
            Content = "取消",
            MinWidth = 96,
        };
        body.Children.Add(new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Spacing = 8,
            Margin = new Thickness(0, 16, 0, 0),
            Children = { primary, cancel },
        });

        var dlg = new ContentDialog
        {
            Content = body,
            XamlRoot = XamlRoot,
        };
        // 只调上限，不设 MinWidth：同时钉死最小和最大宽度会把弹窗推离居中位置
        dlg.Resources["ContentDialogMaxWidth"] = 760.0;

        List<Dictionary<string, object>>? result = null;
        primary.Click += (_, _) =>
        {
            var picked = new List<Dictionary<string, object>>();
            foreach (var it in _preview)
            {
                if (it.HasCutin && it.Cutin) picked.Add(new() { ["card"] = it.Id, ["kind"] = "cutin" });
                if (it.HasRunfx && it.Runfx) picked.Add(new() { ["card"] = it.Id, ["kind"] = "runfx" });
                if (it.HasArt && it.Art) picked.Add(new() { ["card"] = it.Id, ["kind"] = "art" });
            }
            result = picked;
            dlg.Hide();
        };
        cancel.Click += (_, _) => { result = null; dlg.Hide(); };

        await dlg.ShowAsync();
        return result;
    }

    private static string FormatSize(long bytes)
    {
        if (bytes <= 0) return "";
        if (bytes < 1024 * 1024) return $"{bytes / 1024.0:F0} KB";
        return $"{bytes / 1024.0 / 1024.0:F1} MB";
    }

    // --------------------------------------------------------------- 装过的包
    private async Task LoadInstalledAsync()
    {
        if (App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("pack.installed");
            _installed.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("packs", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in arr.EnumerateArray())
                {
                    var id = p.TryGetProperty("id", out var i) ? i.GetString() ?? "" : "";
                    var name = p.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                    var time = p.TryGetProperty("time", out var t) && t.ValueKind == JsonValueKind.Number
                        ? DateTimeOffset.FromUnixTimeSeconds((long)t.GetDouble()).ToLocalTime()
                        : DateTimeOffset.MinValue;
                    // 三类各几份：安装时按卡去重算好放在 kinds 里。装过的包只做记录，
                    // 复原到各功能页按卡做，这里不再显示「可复原几张」那种按钮状态。
                    var parts = new List<string>();
                    if (p.TryGetProperty("kinds", out var ks) && ks.ValueKind == JsonValueKind.Object)
                    {
                        foreach (var kv in ks.EnumerateObject())
                        {
                            if (kv.Value.ValueKind != JsonValueKind.Number) continue;
                            var label = kv.Name switch
                            {
                                "art" => "超框",
                                "cutin" => "动画",
                                "runfx" => "特效",
                                _ => "",
                            };
                            if (label.Length > 0 && kv.Value.GetInt32() > 0)
                                parts.Add($"{label} {kv.Value.GetInt32()}");
                        }
                    }
                    var summary = parts.Count > 0
                        ? string.Join("  ", parts)
                        : "无明细";
                    _installed.Add(new PackInstalledItem
                    {
                        Id = id,
                        Name = name.Length > 0 ? name : "（未命名）",
                        When = time == DateTimeOffset.MinValue ? "" : time.ToString("MM-dd HH:mm"),
                        Summary = summary,
                    });
                }
            }
            var any = _installed.Count > 0;
            InstalledList.Visibility = any ? Visibility.Visible : Visibility.Collapsed;
            InstalledEmpty.Visibility = any ? Visibility.Collapsed : Visibility.Visible;
        }
        catch (Exception ex)
        {
            SetStatus("读取安装记录失败：" + ex.Message);
        }
    }

    // --------------------------------------------------------------- 选路径
    private static IntPtr OwnerHwnd()
        => WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindow!);

    private async Task<string?> PickFolderAsync()
    {
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.PicturesLibrary };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        picker.FileTypeFilter.Add("*");

        var folder = await picker.PickSingleFolderAsync();
        return folder?.Path;
    }

    private async Task<string?> PickPackFileAsync()
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.Downloads };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        picker.FileTypeFilter.Add(".zip");

        var file = await picker.PickSingleFileAsync();
        return file?.Path;
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
            // 只写在底部容易漏看，失败一律弹出来
            await Dialogs.InfoAsync(XamlRoot, "操作失败", ex.Message, danger: true);
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

    /// <summary>空闲时把底部状态条收起。XAML 里默认就是折叠的，别让过期文字常驻。</summary>
    private void ClearStatus()
    {
        StatusText.Text = "";
        StatusStrip.Visibility = Visibility.Collapsed;
    }

    private void SetBusy(bool busy)
    {
        _jobActive = busy;   // 本页所有任务的唯一进出点，进度广播按它放行
        ExportBtn.IsEnabled = !busy;
        InstallBtn.IsEnabled = !busy;
        PackBrowseBtn.IsEnabled = !busy;
        ExportList.IsEnabled = !busy;
        Progress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        Progress.IsIndeterminate = busy;
        if (!busy)
        {
            Progress.Value = 0;
            Progress.Maximum = 100;
        }
    }

    // --------------------------------------------------------------- 引擎事件
    private void OnEngineProgress(string job, string text, int i, int n)
        => DispatcherQueue.TryEnqueue(() =>
        {
            if (!_jobActive) return;   // 别人的任务（比如设置页在下载组件），不听
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
}
