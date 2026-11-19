using System.Text.Json;
using MDStudio.Services;
using Windows.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using Windows.Storage.Pickers;

namespace MDStudio.Pages;

/// <summary>账号下拉项：id 是目录名，label 是「哈希 · 备注名」。</summary>
public sealed record AccountOption(string Id, string Label);

public sealed partial class SettingsPage : Page
{
    private bool _loading;
    /// <summary>本实例已经取过数。页面开了 NavigationCacheMode=Required，切走再回来
    /// 是同一个实例，不该再跑一遍 LoadAsync（否则账号/组件那几块会闪回「读取中…」）。
    /// 设置页内改目录、切账号、重新同步那些按钮会显式调 LoadAsync，不受这个标记影响。</summary>
    private bool _loaded;

    // --------------------------------------------------------------- 额外组件
    /// <summary>一行组件：卡片、状态文字、按钮、进度条。</summary>
    private sealed record AssetRow(string Key, long Size, Border Card,
                                   TextBlock State, Button Action, ProgressBar Bar);

    private readonly List<AssetRow> _rows = new();
    /// <summary>折叠起来的那几项（第一条永远常显）。</summary>
    private readonly List<AssetRow> _moreRows = new();
    private bool _assetsExpanded;

    // --------------------------------------------------------------- 关于统计
    private int _statCutin, _statRunfx, _statArt;
    private double _statBackupMb;
    private int _kindMonster, _kindSpell, _kindTrap;
    /// <summary>最近 30 天，从早到晚，最后一格是今天。</summary>
    private readonly List<(string Date, int Count)> _heat = new();
    private AssetRow? _downloading;
    private string? _focusKey;

    public SettingsPage()
    {
        InitializeComponent();
        SettingsScroller.SizeChanged += (_, _) => SyncBodyWidth();
        ActualThemeChanged += (_, _) => RenderStats();
        Unloaded += (_, _) =>
        {
            App.DryRunChanged -= OnDryRunChanged;
            App.MainBusyChanged -= OnMainBusy;
        };
    }

    /// <summary>标题栏徽标关掉预演时，把这里的开关同步过来。</summary>
    private void OnDryRunChanged()
    {
        if (DrySwitch.IsOn != App.DryRun) DrySwitch.IsOn = App.DryRun;
    }

    private const string BusyHint = "有动作在跑，完成前不能切换账号或更改游戏目录。";

    /// <summary>低清兼容那行说明的两种文案。空闲时讲它做什么，跑任务时讲为什么点不了。</summary>
    private const string SdHintIdle =
        "生成召唤动画时，低清（SD）贴图也一起重写。只写单页贴图，超出上限会提示。"
        + "关掉后再生成，低清那份恢复成官方；整合包里开与不开的卡混装时，"
        + "只有开了的卡在低画质下生效。";
    private const string SdHintBusy = "生成进行中，暂时不能改这个设置。";

    /// <summary>主任务槽进出：跑动作时锁住账号与目录，别让用户切到一半。</summary>
    private void OnMainBusy()
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            bool busy = App.MainBusy;
            AccountBox.IsEnabled = !busy;
            ChangeDirBtn.IsEnabled = !busy;
            DetectBtn.IsEnabled = !busy;
            // 低清兼容决定这次写不写 SD，跑任务时改它会让用户以为对当前这次生效。
            // 引擎在动作开始那一刻就把开关读进参数了，中途改只影响下一次。
            CompatSdSwitch.IsEnabled = !busy;
            CompatSdHint.Text = busy ? SdHintBusy : SdHintIdle;
            if (busy)
                GameHint.Text = BusyHint;
            else if (GameHint.Text == BusyHint)
                GameHint.Text = "";
        });
    }

    /// <summary>把内容宽度钉到视口宽度（上限 1040），否则 ScrollViewer 无限宽测量会让 * 列塌成内容宽。
    /// 1040 是两栏的下限：再窄右栏的组件名就要换行了。</summary>
    private void SyncBodyWidth()
    {
        var w = SettingsScroller.ActualWidth;
        if (double.IsNaN(w) || w <= 0) return;
        var target = Math.Min(w, 1040);
        if (double.IsNaN(SettingsBody.Width) || Math.Abs(SettingsBody.Width - target) > 1)
            SettingsBody.Width = target;
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        // 先退订再订，避免页面被复用时重复挂上同一个处理器
        App.DryRunChanged -= OnDryRunChanged;
        App.DryRunChanged += OnDryRunChanged;
        DrySwitch.IsOn = App.DryRun;
        // 有动作在跑时账号与目录要锁住：那个动作已经在按旧账号写文件了
        App.MainBusyChanged -= OnMainBusy;
        App.MainBusyChanged += OnMainBusy;
        OnMainBusy();
        if (_loaded)
        {
            // 版本号与四项统计都会被别的页面改掉（生成、复原、装包），回来时补一次。
            // 两条都是毫秒级，组件那几块继续沿用上次结果，不会闪回「读取中…」。
            if (App.Engine is { } again)
                await Task.WhenAll(LoadInfoAsync(again), LoadStatsAsync(again));
            return;
        }
        _loaded = true;
        await LoadAsync();
    }

    /// <summary>
    /// 分三批取数，谁先回来谁先上屏。
    ///
    /// 以前是四条请求一起发、最后一起 await，其中最慢的卡片索引要重建 4~11 秒，
    /// 于是「额外运行组件」（2 毫秒就能答）也陪着空白到手。现在按真实耗时分：
    ///   ① 组件 + 账号  —— 两条都是几十毫秒，并行发，谁先回谁先填
    ///   ② 关于        —— 紧跟其后，一条 1 毫秒的查询
    ///   ③ 卡片数据    —— 要重建索引，单独等，好了才填那一栏
    /// 每批都自己 catch，一批失败不影响别的区块显示。
    /// </summary>
    private async Task LoadAsync()
    {
        if (App.Engine is not { } engine)
        {
            GameHint.Text = "引擎没有启动：" + (App.EngineError ?? "未知原因");
            IndexSummary.Text = "引擎没有启动，无法读取卡片数据。";
            return;
        }

        _loading = true;
        try
        {
            // ① 两批同时出发，各自回来就各自填。组件那条先发：
            //    它只查几个文件在不在（实测 2 毫秒），让它排在最前，
            //    用户第一眼看到的就是「组件已装/未装」。
            await Task.WhenAll(LoadAssetsAsync(), LoadAccountAsync(engine));
            // ② 关于：版本号与四项统计，两条都是毫秒级，并行发
            await Task.WhenAll(LoadInfoAsync(engine), LoadStatsAsync(engine));
        }
        catch (Exception ex)
        {
            GameHint.Text = "读取设置失败：" + ex.Message;
        }
        finally
        {
            _loading = false;
        }

        // ③ 卡片数据放最后，且不阻塞上面三块
        await LoadIndexAsync();
    }

    /// <summary>游戏目录与账号。存过的目录还在就只读配置，不重扫盘。</summary>
    private async Task LoadAccountAsync(EngineClient engine)
    {
        try
        {
            var s = await engine.CallAsync("settings.get");
            DirBox.Text = Str(s, "game_dir") is { Length: > 0 } dir ? dir : "（没找到，请手动选择）";

            var opts = new List<AccountOption>();
            if (s.TryGetProperty("accounts", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var a in arr.EnumerateArray())
                    opts.Add(new AccountOption(Str(a, "id"), Str(a, "label")));
            }
            AccountBox.ItemsSource = opts;

            var current = Str(s, "account");
            // 找不到就留空，不要顺手选中第一个。以前那句 ?? opts.FirstOrDefault() 会让
            // 界面显示「已选 A」，而配置里其实是空的或指向 B，账号就这么漂了。
            var match = opts.FirstOrDefault(o => o.Id == current);
            AccountBox.SelectedItem = match;
            NameBox.Text = NicknameOf(match);
            // 「低清纹理兼容」存在同一份设置里，跟账号一起读回来
            _sdSync = true;
            try
            {
                CompatSdSwitch.IsOn = s.TryGetProperty("compat_sd", out var sd)
                                      && sd.ValueKind == JsonValueKind.True;
            }
            finally { _sdSync = false; }

            if (opts.Count == 0)
                GameHint.Text = "这个游戏目录下还没有账号。进游戏登录一次，再回来点「重新检测」。";
            else if (match is null)
                GameHint.Text = "存过的账号目录不在，游戏可能正在更新。等更新完再来，或在这里另选一个。";
            else
                GameHint.Text = opts.Count > 1
                    ? "检测到多个账号。两个账号的游戏文件是两份独立副本，切换账号等于切换历史。"
                    : "";
        }
        catch (Exception ex)
        {
            GameHint.Text = "读取账号失败：" + ex.Message;
        }
    }

    private async Task LoadInfoAsync(EngineClient engine)
    {
        try
        {
            var info = await engine.CallAsync("info");
            VersionText.Text = Str(info, "version");
        }
        catch (Exception ex)
        {
            VersionText.Text = "读取失败：" + ex.Message;
        }
    }

    /// <summary>备注名从 label 里剥出来：label 形如「hash · 主号」。</summary>
    private static string NicknameOf(AccountOption? opt)
    {
        if (opt is null) return "";
        var i = opt.Label.IndexOf(" · ", StringComparison.Ordinal);
        return i < 0 ? "" : opt.Label[(i + 3)..];
    }

    private static string Str(JsonElement obj, string key)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    // --------------------------------------------------------------- 账号
    private async void AccountBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || App.Engine is not { } engine) return;
        if (AccountBox.SelectedItem is not AccountOption opt) return;

        try
        {
            // 切账号 = 换一套历史，后端会顺带把卡名表和列表缓存清掉
            await engine.CallAsync("settings.set", new { account = opt.Id });
            NameBox.Text = NicknameOf(opt);
            GameHint.Text = "已切到 " + opt.Label;
            // 主窗口那层「没选账号」的提示要跟着撤掉
            _ = (App.MainWindow as MainWindow)?.RefreshReadinessAsync();
        }
        catch (Exception ex)
        {
            // 没切成功（多半是还有动作在跑）：把下拉拨回当前生效的账号，
            // 否则界面显示成一个其实没切过去的值，后面的操作全按旧账号走
            _loading = true;
            try { await LoadAccountAsync(engine); }
            finally { _loading = false; }
            GameHint.Text = "切换账号失败：" + ex.Message;
        }
    }

    private async void SaveName_Click(object sender, RoutedEventArgs e)
    {
        if (AccountBox.SelectedItem is not AccountOption opt || App.Engine is not { } engine) return;
        try
        {
            await engine.CallAsync("settings.set", new { account = opt.Id, name = NameBox.Text.Trim() });
            // 只重读账号这一栏（一条 settings.get，几毫秒）。以前这里调 LoadAsync()，
            // 会连带把组件、卡片数据整块重跑一遍：改个备注名不该让下面的卡牌数据闪一次。
            _loading = true;
            try { await LoadAccountAsync(engine); }
            finally { _loading = false; }
            GameHint.Text = "备注名已保存";
        }
        catch (Exception ex)
        {
            GameHint.Text = "保存失败：" + ex.Message;
        }
    }

    // --------------------------------------------------------------- 目录
    private async void PickDir_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;

        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.ComputerFolder };
        WinRT.Interop.InitializeWithWindow.Initialize(
            picker, WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindow!));
        picker.FileTypeFilter.Add("*");

        var folder = await picker.PickSingleFolderAsync();
        if (folder is null) return;

        // 选错目录以前会直接存下来，之后每个功能都报「找不到游戏文件」。
        // 先验一次，不对就弹窗说明并且什么都不改。
        if (!await ImportCheck.EnsureAsync(XamlRoot, folder.Path, "game_dir")) return;

        try
        {
            await engine.CallAsync("settings.set", new { game_dir = folder.Path });
            GameHint.Text = "已切换游戏目录";
        }
        catch (Exception ex)
        {
            GameHint.Text = "切换失败：" + ex.Message;
            await Dialogs.InfoAsync(XamlRoot, "切换目录失败", ex.Message, danger: true);
        }
        await LoadAsync();
    }

    private async void Detect_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        try
        {
            // force_detect：缓存里那份可能已经过时（用户挪过游戏目录），手动点就该真重扫
            var s = await engine.CallAsync("settings.get", new { force_detect = true });
            var found = Str(s, "detected");
            if (found.Length == 0)
            {
                GameHint.Text = "没自动找到游戏目录，请用「更改…」手动选择。";
                return;
            }
            await engine.CallAsync("settings.set", new { game_dir = found });
            GameHint.Text = "已使用检测到的目录";
        }
        catch (Exception ex)
        {
            GameHint.Text = "重新检测失败：" + ex.Message;
        }
        await LoadAsync();
    }

    // --------------------------------------------------------------- 重新同步
    private async void Resync_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;

        ResyncButton.IsEnabled = false;
        ResyncBusy.Visibility = Visibility.Visible;
        ResyncRing.Visibility = Visibility.Visible;
        ResyncStatus.Text = App.DryRun ? "检查中（预演）…" : "检查中…";

        try
        {
            var data = await engine.CallAsync("tx.resync", new { dry_run = App.DryRun });
            ResyncRing.Visibility = Visibility.Collapsed;
            await ShowResyncResultAsync(data);
            await LoadAsync();
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            ResyncRing.Visibility = Visibility.Collapsed;
            ResyncStatus.Text = "已取消";
        }
        catch (Exception ex)
        {
            ResyncRing.Visibility = Visibility.Collapsed;
            ResyncStatus.Text = "失败：" + ex.Message;
            await InfoAsync("重新同步失败", ex.Message, danger: true);
        }
        finally
        {
            ResyncButton.IsEnabled = true;
        }
    }

    /// <summary>把 resync 报告翻成人话。只有需要用户动手的项才弹窗，其余进状态行。</summary>
    private async Task ShowResyncResultAsync(JsonElement r)
    {
        var cutins = Strings(r, "cutin");
        var fx = Strings(r, "runfx");
        var fixes = Int(r, "catalog");
        var moved = Strings(r, "moved").Count;
        var lost = Strings(r, "lost_files");
        // 卡图登记表只修一种：我们自己做过的卡丢了登记。
        var art = Strings(r, "overframe").Count;

        // broken 的键是卡号，值是缺的依赖名。按卡聚起来才是用户能看懂的单位，
        // 逐个文件报 88 条 hash 路径没人看得下去。
        var broken = new List<string>();
        if (r.TryGetProperty("broken", out var b) && b.ValueKind == JsonValueKind.Object)
            broken.AddRange(b.EnumerateObject().Select(p => p.Name));

        if (cutins.Count == 0 && fx.Count == 0 && fixes == 0 && moved == 0
            && lost.Count == 0 && art == 0)
        {
            ResyncStatus.Text = "没有需要补的，游戏清单和历史现在一致。";
            return;
        }

        // 只报真正修了的项，避免"补回召唤动画 0 处、发动特效 0 处"这种话
        var parts = new List<string>();
        if (cutins.Count > 0) parts.Add($"补回召唤动画 {cutins.Count} 处");
        if (fx.Count > 0) parts.Add($"补回发动特效 {fx.Count} 处");
        if (fixes > 0) parts.Add($"修订清单 {fixes} 项");
        if (moved > 0) parts.Add($"搬移文件 {moved} 个");
        if (art > 0) parts.Add($"重新登记超框 {art} 张");
        ResyncStatus.Text = App.DryRun
            ? "预演结束，没有写游戏文件"
            : parts.Count > 0 ? string.Join("，", parts) : "检查完成，有几处需要处理";

        var todo = new List<string>();
        if (broken.Count > 0)
        {
            var cards = broken.Count > 8
                ? string.Join("、", broken.Take(8)) + $" 等 {broken.Count} 张"
                : string.Join("、", broken);
            todo.Add($"以下卡缺文件，需要移除后重新添加：{cards}");
        }
        if (lost.Count > 0 && broken.Count == 0)
            todo.Add($"有 {lost.Count} 个文件在更新后不见了，相关卡需要重做。");

        foreach (var c in Strings(r, "game_took_over"))
            todo.Add($"卡 {c}：游戏已自带官方版本，你的改动被跳过。");
        foreach (var lb in Strings(r, "sound_reset"))
            todo.Add($"音效 {lb}：游戏更新换掉了原文件，你的 WAV 需要重新注入。");

        // 结果改成弹窗：上面写这次补了什么，下面提示条写要你处理什么。
        // 只有底部一行字的话，跑完这几十项修复用户根本不知道发生过什么。
        var body = parts.Count > 0
            ? "这次补了：" + string.Join("、", parts) + "。"
            : "这次没有改动游戏文件。";
        if (App.DryRun) body += "\n预演模式：没有写游戏文件。";

        var title = todo.Count > 0 ? "同步完成，有几处需要处理" : "同步完成";
        if (App.DryRun) title = title.Replace("同步", "预演");

        await InfoAsync(title, body,
            danger: broken.Count > 0,
            note: todo.Count > 0 ? string.Join("\n", todo) : null);
    }

    private static List<string> Strings(JsonElement obj, string key)
    {
        var list = new List<string>();
        if (obj.ValueKind != JsonValueKind.Object ||
            !obj.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array)
            return list;
        foreach (var v in arr.EnumerateArray())
            list.Add(v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : v.ToString());
        return list;
    }

    private static int Int(JsonElement obj, string key)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt32() : 0;

    // 样式统一走 Services.Dialogs，跟卡图页、动画页同一套骨架。
    private Task InfoAsync(string title, string body, bool danger = false, string? note = null)
        => Dialogs.InfoAsync(XamlRoot, title, body, danger, null, null, note);

    // --------------------------------------------------------------- 卡片数据
    /// <summary>重建中：挡住重复点击，也避免和「重新同步」抢引擎线程。</summary>
    private bool _indexBusy;

    private async Task LoadIndexAsync()
    {
        if (App.Engine is not { } engine) return;

        // 冷启动时索引要重建，这一段是唯一会让人等的查询。
        // 订阅 IndexProgressChanged（私有事件）而不是 ProgressChanged：
        // 后者动画页也一直挂着，把索引进度混进去会让它的状态条乱闪。
        //
        // _indexLoading 是必要的：进度事件走 DispatcherQueue 异步投递，回调可能
        // 在查询已经返回、面板已经收起之后才执行，那时再画就会留下一条永远转圈的
        // 进度条（缓存命中只要 2 毫秒，正好踩这个坑）。
        _indexLoading = true;
        void OnProgress(string text, int i, int n)
            => DispatcherQueue.TryEnqueue(() =>
            {
                if (_indexLoading) ShowIndexStage(text, i, n);
            });

        engine.IndexProgressChanged += OnProgress;
        try
        {
            ApplyIndexInfo(await engine.CallAsync("cards.index_info"));
        }
        catch (Exception ex)
        {
            IndexSummary.Text = "读取卡片数据状态失败：" + ex.Message;
            IndexBuilt.Text = "";
        }
        finally
        {
            _indexLoading = false;
            engine.IndexProgressChanged -= OnProgress;
            IndexProgressPanel.Visibility = Visibility.Collapsed;
        }
    }

    private bool _indexLoading;

    /// <summary>索引构建进度：阶段文字 + 能算进度的阶段给百分比。</summary>
    private void ShowIndexStage(string text, int i, int n)
    {
        IndexProgressPanel.Visibility = Visibility.Visible;
        IndexBar.IsIndeterminate = i < 0 || n <= 0;
        if (!IndexBar.IsIndeterminate) IndexBar.Value = Math.Clamp(i * 100.0 / n, 0, 100);
        IndexStage.Text = i >= 0 && n > 0
            ? $"{text}　{i:N0} / {n:N0}"
            : text + "…";
    }

    /// <summary>正常只留一行摘要；读失败或有未识别卡型时才升 InfoBar。</summary>
    private void ApplyIndexInfo(JsonElement info)
    {
        var cards = Int(info, "cards");
        var visible = Int(info, "visible");
        var built = Str(info, "built_at");
        var error = Str(info, "error");
        var unknown = Strings(info, "unknown_subtypes");
        var unparsed = Strings(info, "unparsed_sources");
        var log = Str(info, "warning_log");

        if (!Flag(info, "ok"))
        {
            IndexSummary.Text = $"{visible:N0} 张可用";
            IndexBuilt.Text = "";
            IndexProblem.Severity = InfoBarSeverity.Error;
            IndexProblem.Title = "卡片数据读取失败";
            IndexProblem.Message = error.Length > 0
                ? error
                : "游戏文件可能已经变化，点右侧重建试试。";
            IndexProblem.IsOpen = true;
            RebuildIndexButton.Content = "重试";
            return;
        }

        // 秒对用户没意义，只留到分钟
        var when = built.Length >= 16 ? built[..16] : built;
        // 窄栏里一行放不下那句长话，拆成两行；卡号总数（含预留号）对用户没意义，挪到悬停
        IndexSummary.Text = $"{visible:N0} 张可用";
        IndexBuilt.Text = when.Length >= 16 ? $"构建于 {when[5..16]}" : $"构建于 {when}";
        ToolTipService.SetToolTip(IndexSummary, $"共 {cards:N0} 个卡号");
        RebuildIndexButton.Content = "重建卡片数据";

        if (unknown.Count == 0 && unparsed.Count == 0)
        {
            IndexProblem.IsOpen = false;
            return;
        }

        var parts = new List<string>();
        if (unknown.Count > 0)
            parts.Add($"有 {unknown.Count} 个卡型未识别（{string.Join("、", unknown.Take(6))}），"
                      + "相关卡只能预览，不能写入。");
        if (unparsed.Count > 0)
            parts.Add($"{unparsed.Count} 个数据表没读到（{string.Join("、", unparsed.Take(4))}），"
                      + "卡片信息可能不完整。");
        if (log.Length > 0) parts.Add("日志：" + log);

        IndexProblem.Severity = InfoBarSeverity.Warning;
        IndexProblem.Title = "卡片数据不完整";
        IndexProblem.Message = string.Join("\n", parts);
        IndexProblem.IsOpen = true;
    }

    private async void RebuildIndex_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine || _indexBusy) return;

        _indexBusy = true;
        RebuildIndexButton.IsEnabled = false;
        RebuildIndexButton.Content = "重建中…";
        IndexSummary.Text = "正在重读游戏数据，大约几秒…";
        IndexBuilt.Text = "";
        IndexProblem.IsOpen = false;
        IndexProgressPanel.Visibility = Visibility.Visible;
        IndexBar.IsIndeterminate = true;
        _indexLoading = true;

        void OnProgress(string text, int i, int n)
            => DispatcherQueue.TryEnqueue(() =>
            {
                if (_indexLoading) ShowIndexStage(text, i, n);
            });

        engine.IndexProgressChanged += OnProgress;
        try
        {
            ApplyIndexInfo(await engine.CallAsync("cards.refresh"));
        }
        catch (Exception ex)
        {
            IndexSummary.Text = "重建失败：" + ex.Message;
            IndexBuilt.Text = "";
            RebuildIndexButton.Content = "重试";
        }
        finally
        {
            _indexLoading = false;
            engine.IndexProgressChanged -= OnProgress;
            IndexProgressPanel.Visibility = Visibility.Collapsed;
            _indexBusy = false;
            RebuildIndexButton.IsEnabled = true;
        }
    }

    // --------------------------------------------------------------- 额外组件
    /// <summary>重新读组件状态并重建列表。每次进设置页都刷一次，用户可能在别处删了目录。</summary>
    private async Task LoadAssetsAsync()
    {
        if (App.Engine is not { } engine) return;

        JsonElement st;
        try
        {
            st = await engine.CallAsync("assets.status");
        }
        catch (Exception ex)
        {
            AssetsPanel.Children.Clear();
            _rows.Clear();
            AssetsPanel.Children.Add(new TextBlock
            {
                Text = "读取组件状态失败：" + ex.Message,
                TextWrapping = TextWrapping.Wrap,
                Style = Res<Style>("CaptionTextBlockStyle"),
            });
            return;
        }

        AssetsPanel.Children.Clear();
        AssetsMore.Children.Clear();
        _rows.Clear();
        _moreRows.Clear();
        if (st.ValueKind != JsonValueKind.Object ||
            !st.TryGetProperty("assets", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return;

        foreach (var a in arr.EnumerateArray())
        {
            var row = BuildAssetRow(Str(a, "key"), Str(a, "label"), Num(a, "size"), Flag(a, "present"));
            _rows.Add(row);
            // 第一条常显，其余收进 AssetsMore：折叠时它跟「高级设置」正好等高
            if (_rows.Count == 1) AssetsPanel.Children.Add(row.Card);
            else AssetsMore.Children.Add(row.Card);
        }
        _moreRows.AddRange(_rows.Skip(1));
        SetAssetsExpanded(false);

        await ApplyFocusAsync();
    }

    private AssetRow BuildAssetRow(string key, string label, long size, bool present)
    {
        var head = new Grid { ColumnSpacing = 12 };
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var texts = new StackPanel { Spacing = 2 };
        texts.Children.Add(new TextBlock
        {
            Text = label,
            Style = Res<Style>("BodyStrongTextBlockStyle"),
        });
        texts.Children.Add(new TextBlock
        {
            Text = $"{size / 1024.0 / 1024.0:F0} MB",
            Style = Res<Style>("CaptionTextBlockStyle"),
            Foreground = Res<Brush>("TextFillColorSecondaryBrush"),
        });

        var state = new TextBlock
        {
            VerticalAlignment = VerticalAlignment.Center,
            Style = Res<Style>("CaptionTextBlockStyle"),
            Text = present ? "已安装" : "未安装",
            Foreground = present
                ? Res<Brush>("SystemFillColorSuccessBrush")
                : Res<Brush>("TextFillColorSecondaryBrush"),
        };
        Grid.SetColumn(state, 1);

        var action = new Button
        {
            Content = "下载",
            MinWidth = 76,
            VerticalAlignment = VerticalAlignment.Center,
            Visibility = present ? Visibility.Collapsed : Visibility.Visible,
        };
        Grid.SetColumn(action, 2);

        head.Children.Add(texts);
        head.Children.Add(state);
        head.Children.Add(action);

        var bar = new ProgressBar
        {
            Minimum = 0, Maximum = 100, Value = 0,
            Height = 4,
            CornerRadius = new CornerRadius(2),
            Visibility = Visibility.Collapsed,
        };

        var inner = new StackPanel { Spacing = 8 };
        inner.Children.Add(head);
        inner.Children.Add(bar);

        var card = new Border
        {
            Padding = new Thickness(14, 10, 14, 10),
            CornerRadius = new CornerRadius(6),
            Background = Res<Brush>("CardBackgroundFillColorDefaultBrush"),
            BorderBrush = Res<Brush>("CardStrokeColorDefaultBrush"),
            BorderThickness = new Thickness(1),
            Child = inner,
        };

        var row = new AssetRow(key, size, card, state, action, bar);
        action.Click += async (_, _) =>
        {
            if (_downloading == row) { await CancelDownloadAsync(); return; }
            await DownloadAsync(row);
        };
        return row;
    }

    private async Task DownloadAsync(AssetRow row)
    {
        if (App.Engine is not { } engine || _downloading is not null) return;

        _downloading = row;
        row.Bar.Value = 0;
        row.Bar.Visibility = Visibility.Visible;
        row.State.Foreground = Res<Brush>("TextFillColorSecondaryBrush");
        row.State.Text = "连接中…";
        row.Action.Content = "取消";
        foreach (var r in _rows)
            if (r != row) r.Action.IsEnabled = false;

        // 这个回调跑在引擎的读线程上，直接改界面会抛跨线程异常；
        // 而且抛在这里会把引擎的读循环带死，之后所有调用（包括取消）就再也等不到回应。
        // 只认自己的 op：下载跑在轻任务槽里，用户随时能切到别的页去写超框/生成动画，
        // 那些动作报的阶段文字也走这条通道（2026-11-12 用户实测被串过）。
        void OnProgress(string job, string text, int i, int n)
            => DispatcherQueue.TryEnqueue(() =>
            {
                if (job != "assets.ensure") return;
                if (n <= 0)
                {
                    // 阶段提示（比如测速、校验、重试），没有百分比
                    row.State.Text = text.Length > 0 ? text : "准备中…";
                    return;
                }
                row.Bar.Value = Math.Clamp(i * 100.0 / n, 0, 100);
                row.State.Text = $"{i * 100.0 / n:F0}%";
            });

        engine.ProgressChanged += OnProgress;
        try
        {
            await engine.CallAsync("assets.ensure", new { key = row.Key });
            row.Bar.Visibility = Visibility.Collapsed;
            row.State.Text = "已安装";
            row.State.Foreground = Res<Brush>("SystemFillColorSuccessBrush");
            row.Action.Visibility = Visibility.Collapsed;
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            row.Bar.Visibility = Visibility.Collapsed;
            row.State.Text = "已取消";
        }
        catch (Exception ex)
        {
            row.Bar.Visibility = Visibility.Collapsed;
            row.State.Text = "失败";
            row.State.Foreground = Res<Brush>("SystemFillColorCriticalBrush");
            // 只有中断类失败能续传，校验不过、写不进去那些重试也是白搭，
            // 所以「再点一次就能接着下」只在真能续上的时候说。
            await InfoAsync("下载失败", ex.Message, danger: true,
                note: (ex as EngineException)?.Kind == "AssetInterrupted"
                    ? "已经下好的部分会保留，再点一次「下载」就能接着下。"
                    : null);
        }
        finally
        {
            engine.ProgressChanged -= OnProgress;
            _downloading = null;
            row.Action.Content = "下载";
            foreach (var r in _rows) r.Action.IsEnabled = true;
        }
    }

    /// <summary>
    /// 额外组件折叠。结构是「第一条常显 + 其余可收起」：折叠时两张卡都是
    /// 标题 + 一行，高度自然一致；以后高级设置加第二项也照这个来。
    /// </summary>
    private void AssetsMore_Click(object sender, RoutedEventArgs e) => SetAssetsExpanded(!_assetsExpanded);

    private void SetAssetsExpanded(bool on)
    {
        _assetsExpanded = on;
        AssetsMore.Visibility = on ? Visibility.Visible : Visibility.Collapsed;
        if (_rows.Count <= 1)
        {
            AssetsMoreBtn.Visibility = Visibility.Collapsed;   // 只有一项，没什么可收的
            return;
        }
        AssetsMoreBtn.Visibility = Visibility.Visible;
        var text = new TextBlock { Text = on ? "收起" : $"还有 {_rows.Count - 1} 项", FontSize = 12 };
        var glyph = new FontIcon
        {
            Glyph = on ? "\uE70E" : "\uE70D",
            FontSize = 10,
            Margin = new Thickness(5, 0, 0, 0),
        };
        var panel = new StackPanel { Orientation = Orientation.Horizontal };
        panel.Children.Add(text);
        panel.Children.Add(glyph);
        AssetsMoreBtn.Content = panel;
    }

    private async Task CancelDownloadAsync()
    {
        if (App.Engine is not { } engine) return;
        try
        {
            await engine.CallAsync("cancel");
        }
        catch
        {
            // 取消失败就让下载自己跑完或失败，不算致命
        }
    }

    /// <summary>
    /// 从别的页面跳过来时定位到某个组件（比如 Spine 提示里点了「去下载」）。
    /// 页面可能是刚创建的，列表还没建好，所以这里等一会儿再找。
    /// </summary>
    public void FocusAsset(string? key)
    {
        _focusKey = key;
        _ = ApplyFocusAsync();
    }

    private async Task ApplyFocusAsync()
    {
        if (_focusKey is not { } key) return;

        // 首次进页面要等引擎返回状态，给足两秒
        for (int i = 0; i < 40 && _rows.Count == 0; i++)
            await Task.Delay(50);

        var row = _rows.FirstOrDefault(r => r.Key == key);
        if (row is null) return;
        _focusKey = null;

        // 从别处跳过来装组件：整张清单都展开。只展开目标那一行的话，
        // 同时缺的另一个还收在「还有 N 项」里，用户装完一个回去又会被拦一次
        if (!_assetsExpanded && _rows.Count > 1)
        {
            SetAssetsExpanded(true);
            await Task.Delay(120);      // 等布局算完，否则滚到的还是折叠前的位置
        }

        await Task.Delay(80);      // 等布局算完再滚，否则目标位置还是 0
        row.Card.StartBringIntoView(new BringIntoViewOptions
        {
            AnimationDesired = true,
            VerticalAlignmentRatio = 0.15,
        });

        // 闪一下边框，告诉用户"就是这一项"
        row.Card.BorderBrush = Res<Brush>("AccentFillColorDefaultBrush");
        row.Card.BorderThickness = new Thickness(2);
        await Task.Delay(1600);
        row.Card.BorderThickness = new Thickness(1);
        row.Card.BorderBrush = Res<Brush>("CardStrokeColorDefaultBrush");
    }

    private static T? Res<T>(string key) where T : class
        => Application.Current.Resources.TryGetValue(key, out var v) ? v as T : null;

    private static bool Flag(JsonElement obj, string key)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.True;

    private static long Num(JsonElement obj, string key)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt64() : 0;

    // --------------------------------------------------------------- 关于统计
    /// <summary>四个数：三类改动各几张卡、备份占多大、最近 30 天每天几次。</summary>
    private async Task LoadStatsAsync(EngineClient engine)
    {
        try
        {
            var s = await engine.CallAsync("stats");
            _statCutin = Int(s, "cutin");
            _statRunfx = Int(s, "runfx");
            _statArt = Int(s, "art");
            _statBackupMb = s.TryGetProperty("backup_mb", out var b) && b.ValueKind == JsonValueKind.Number
                ? b.GetDouble() : 0;
            if (s.TryGetProperty("kinds", out var k))
            {
                _kindMonster = Int(k, "monster");
                _kindSpell = Int(k, "spell");
                _kindTrap = Int(k, "trap");
            }
            _heat.Clear();
            if (s.TryGetProperty("days", out var arr) && arr.ValueKind == JsonValueKind.Array)
                foreach (var d in arr.EnumerateArray())
                    _heat.Add((Str(d, "date"), Int(d, "count")));
            RenderStats();
        }
        catch (Exception ex)
        {
            StatCutinText.Text = "—";
            StatRunfxText.Text = "—";
            StatArtText.Text = "—";
            StatBackupText.Text = "—";
            HeatTotalText.Text = "读取失败：" + ex.Message;
        }
    }

    private void RenderStats()
    {
        StatCutinText.Text = _statCutin.ToString();
        StatRunfxText.Text = _statRunfx.ToString();
        StatArtText.Text = _statArt.ToString();
        StatBackupText.Text = _statBackupMb.ToString("N0");

        int total = _statCutin + _statRunfx + _statArt;
        MixBar.Visibility = total > 0 ? Visibility.Visible : Visibility.Collapsed;
        MixLegend.Visibility = total > 0 ? Visibility.Visible : Visibility.Collapsed;
        MixEmpty.Visibility = total == 0 ? Visibility.Visible : Visibility.Collapsed;
        if (total > 0)
        {
            // 三段按数量分宽度。0 的那一段给一个极小的星权重，免得它把圆角吃掉
            var parts = new[] { _statCutin, _statRunfx, _statArt };
            for (int i = 0; i < 3; i++)
                MixBar.ColumnDefinitions[i].Width =
                    new GridLength(Math.Max(parts[i], 0.0001), GridUnitType.Star);
            MixCutinText.Text = $"动画 {Share(_statCutin, total)}";
            MixRunfxText.Text = $"特效 {Share(_statRunfx, total)}";
            MixArtText.Text = $"超框 {Share(_statArt, total)}";
        }
        RenderKinds();
        RenderHeat();
    }

    private static string Share(int n, int total) => $"{Math.Round(n * 100.0 / total)}%";

    /// <summary>三类卡的数量条。三项共用一把尺子（按最大值归一），长度才可比。</summary>
    private void RenderKinds()
    {
        // 轨道底色按主题给：浅色用浅灰，深色用深灰。写死一个色在另一种主题下会看不见
        var track = new SolidColorBrush(ActualTheme == ElementTheme.Dark
            ? Rgb(0x2E3135)
            : Rgb(0xEEEEEE));
        KindMonsterTrack.Background = track;
        KindSpellTrack.Background = track;
        KindTrapTrack.Background = track;

        int monster = _kindMonster, spell = _kindSpell, trap = _kindTrap;
        int max = Math.Max(monster, Math.Max(spell, trap));
        ApplyBar(KindMonsterBar, KindMonsterText, monster, max);
        ApplyBar(KindSpellBar, KindSpellText, spell, max);
        ApplyBar(KindTrapBar, KindTrapText, trap, max);
    }

    private static void ApplyBar(Grid bar, TextBlock value, int n, int max)
    {
        value.Text = n.ToString("N0");
        var fill = bar.ColumnDefinitions[0];
        var rest = bar.ColumnDefinitions[1];
        if (max <= 0)
        {
            fill.Width = new GridLength(0);
            rest.Width = new GridLength(1, GridUnitType.Star);
            return;
        }
        // 给一个极小的下限：某项为 0 时那一列宽度不能正好 0，否则圆角会歪
        fill.Width = new GridLength(Math.Max(n, 0.0001), GridUnitType.Star);
        rest.Width = new GridLength(Math.Max(max - n, 0.0001), GridUnitType.Star);
    }

    /// <summary>
    /// 30 天热力图：3 行 10 列，从左上往右下铺，最后一格是今天。
    /// 今天那格加一圈描边，就算当天没干活（格子是灰的）也能一眼找到。
    /// </summary>
    private void RenderHeat()
    {
        HeatGrid.Children.Clear();
        HeatGrid.RowDefinitions.Clear();
        HeatGrid.ColumnDefinitions.Clear();
        for (int c = 0; c < 10; c++)
            HeatGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(30) });
        for (int r = 0; r < 3; r++)
            HeatGrid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(30) });

        var palette = HeatPalette();
        int sum = 0;
        for (int i = 0; i < _heat.Count && i < 30; i++)
        {
            var (date, count) = _heat[i];
            sum += count;
            bool today = i == _heat.Count - 1;
            var cell = new Border
            {
                Width = 30,
                Height = 30,
                CornerRadius = new CornerRadius(4),
                Background = new SolidColorBrush(palette[Level(count)]),
            };
            if (today)
            {
                cell.BorderThickness = new Thickness(2);
                cell.BorderBrush = Res<Brush>("TextFillColorPrimaryBrush")
                                   ?? new SolidColorBrush(Color.FromArgb(255, 32, 32, 32));
            }
            string shortDate = date.Length >= 10 ? date[5..] : date;
            ToolTipService.SetToolTip(cell, today
                ? $"今天 · {count} 次"
                : count > 0 ? $"{shortDate} · {count} 次" : $"{shortDate} · 没有改动");
            Grid.SetColumn(cell, i % 10);
            Grid.SetRow(cell, i / 10);
            HeatGrid.Children.Add(cell);
        }

        HeatTotalText.Text = $"共 {sum} 次";
        var keys = new[] { HeatKey1, HeatKey2, HeatKey3, HeatKey4, HeatKey5 };
        for (int i = 0; i < keys.Length; i++) keys[i].Background = new SolidColorBrush(palette[i]);
    }

    /// <summary>次数 → 颜色档位。阈值是固定的，不随当月最高值浮动（浮动的话同一张图两个月没法比）。</summary>
    private static int Level(int count) => count <= 0 ? 0 : count == 1 ? 1 : count <= 3 ? 2 : count <= 6 ? 3 : 4;

    private Color[] HeatPalette()
    {
        bool dark = ActualTheme == ElementTheme.Dark;
        return dark
            ? new[] { Rgb(0x2A2E33), Rgb(0x0E4429), Rgb(0x006D32), Rgb(0x26A641), Rgb(0x39D353) }
            : new[] { Rgb(0xEBEDF0), Rgb(0xC6E8CE), Rgb(0x9BE9A8), Rgb(0x40C463), Rgb(0x216E39) };
    }

    private static Color Rgb(uint v)
        => Color.FromArgb(255, (byte)(v >> 16), (byte)(v >> 8), (byte)v);

    // --------------------------------------------------------------- 低清纹理
    /// <summary>回写中被我们自己改的一瞬间别当成用户操作，否则会转圈回写。</summary>
    private bool _sdSync;

    /// <summary>
    /// 生成召唤动画时连低清（SD）贴图一起重写。
    ///
    /// 存进 config/settings.json，引擎在生成时自己读（settings.compat_sd），
    /// 所以界面上不用把它传给动画页：视频、帧文件夹、Spine 导入三条路同一个值。
    /// </summary>
    private async void CompatSdSwitch_Toggled(object sender, RoutedEventArgs e)
    {
        if (_sdSync || _loading || App.Engine is not { } engine) return;
        bool want = CompatSdSwitch.IsOn;
        try
        {
            var after = await engine.CallAsync("settings.set", new { compat_sd = want });
            bool saved = after.TryGetProperty("compat_sd", out var v) && v.ValueKind == JsonValueKind.True;
            if (saved == want) return;
            // 没存下来：把开关拨回去，别让界面显示的和实际生效的不一样
            _sdSync = true;
            try { CompatSdSwitch.IsOn = saved; }
            finally { _sdSync = false; }
            await InfoAsync("设置没存下来", "配置文件写不进去，这次改动只在界面上。", danger: true);
        }
        catch (Exception ex)
        {
            _sdSync = true;
            try { CompatSdSwitch.IsOn = !want; }
            finally { _sdSync = false; }
            await InfoAsync("设置没存下来", ex.Message, danger: true);
        }
    }

    // --------------------------------------------------------------- 预演
    private void DrySwitch_Toggled(object sender, RoutedEventArgs e)
    {
        App.DryRun = DrySwitch.IsOn;
        (App.MainWindow as MainWindow)?.UpdateDryRunBadge();
    }
}
