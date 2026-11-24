using System.Collections.ObjectModel;
using System.Text.Json;
using MDStudio.Models;
using MDStudio.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using Windows.Globalization.NumberFormatting;
using Windows.Media.Core;
using Windows.Media.Playback;
using Windows.Storage.Pickers;
using Windows.System;

namespace MDStudio.Pages;

/// <summary>卡片主从页：左边列表，右边这张卡的详情与操作。</summary>
public sealed partial class AnimPage : Page
{
    /// <summary>
    /// 源卡预设（下拉内容）。编号不在这里写死：后端 cutin.donors 与「类型→模板卡」
    /// 共用同一张表，界面以前抄了一份旧编号，换表后两边对不上（2026-11-12 用户实测）。
    /// </summary>
    private readonly List<(string Label, int Id)> _donorPresets = new();
    private bool _donorSyncing;

    /// <summary>Silvicola 弹出曲线预设</summary>
    // 「效果」选默认时传这条。与引擎 core/mdstudio/ops/video/cutin_flip.py 的
    // DEFAULT_POP_CURVE 保持同一套数值：最大 108%，收尾停在 90%（2026-11-20 对齐）
    /// <summary>
    /// 召唤场景里屏幕高度等于多少个骨骼单位。2026-11-21 用两档尺寸（2930 / 1465）
    /// 各录一段游戏视频反推，结果 2910 与 2913，取 2880 与播放器保持一致。
    /// 界面按「占屏高倍数」显示，存盘与引擎仍用骨骼单位，两边都无损。
    /// </summary>
    private const double ScreenHeightUnits = 2880.0;

    private static double UnitsToHeight(double units) => units / ScreenHeightUnits;
    private static double HeightToUnits(double height) => height * ScreenHeightUnits;

    private const string CurveSilvicola =
        "(0,25,60)(6,55,75)(15,92,92)(23,108,100)(31,76,100)(68,90,100)";

    private List<CardItem> _donorCandidates = new();
    private int _donorFilledFor = -1;   // 自动填过来源卡的卡号：换卡才重填，用户改过就保留
    private List<SchemeItem> _schemes = new();
    private bool _schemeLoading;
    /// <summary>上一次真正套用过的方案下标。下拉收起时靠它区分「换了方案」与「只是展开看一眼」。</summary>
    private int _appliedScheme = -1;
    /// <summary>特效生成与 Spine 导入的入口守卫，动画那边用的是 _checking。</summary>
    private bool _fxChecking;
    private bool _spineChecking;
    /// <summary>上一次显示过的卡号与模式，用来判断「换了张卡」。0 表示还没显示过任何卡。</summary>
    private int _lastShownId;
    private string _lastShownMode = "";
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _carryTimer;
    /// <summary>当前那条「沿用参数」挂在哪一行，四秒后照着它收起来。</summary>
    private TextBlock? _carryTarget;
    // 自己有任务在跑才响应引擎进度广播。进度事件是全局广播，设置页下载组件时
    // 也会发事件（2026-11-11 用户实测：动画页底部出现"正在下载 %s"和别人的进度条）。
    // 本页常驻订阅不退订（切走要保自己的进度流），所以只能靠这个标志过滤。
    private bool _jobActive;
    /// <summary>当前选中的卡是来源卡：生成与导入按钮要锁死，任务收尾时按它复原按钮。</summary>
    private bool _donorLocked;
    /// <summary>正在跑生成前的检查。入口守卫：检查期间重复点击直接忽略，
    /// 免得两次预检都去弹框（同一 XamlRoot 只允许一个 ContentDialog）。</summary>
    private bool _checking;
    /// <summary>「效果 = 自定义」时用户填的曲线串，存在内存里，不写进每个 NumberBox。</summary>
    private string _customCurve = "";

    /// <summary>
    /// 视频自带的声音要落进一个游戏里已经存在的音效槽。全新的 label 在游戏里不响，
    /// 只能借一个现成的。这个值是后端 runfx_flip.DEFAULT_CARRIER。
    /// </summary>
    private const string FxCarrier = "SE_EV_EF09279_v1";

    /// <summary>一次最多列多少张。真实总数由后端 count 给，这个只管列表长度。</summary>
    private const int SearchLimit = 200;

    private readonly ObservableCollection<CardItem> _shown = new();
    private readonly List<SoundSlot> _slots = new();
    private List<CardItem> _all = new();
    /// <summary>本地有预览缓存的卡号，动画与特效各一份。null = 没问到，一律当作有，退回旧行为。</summary>
    private HashSet<int>? _previewCutin;
    private HashSet<int>? _previewRunfx;
    private CardItem? _current;
    /// <summary>有任务在跑。跑的时候整张表单锁死，也不许换卡。</summary>
    private bool _jobRunning;
    /// <summary>正在把列表选中项拨回原来那张，避免 SelectionChanged 递归。</summary>
    private bool _revertingSelection;
    private bool _slotsLoaded;
    // 随包的预设音效, 只有几条, 读一次就够
    private readonly List<PresetSound> _presets = new();
    private bool _presetsLoaded;
    // 试听用: 播的是源文件(预设或刚导入的那份), 跟写进游戏的是同一段音频
    private MediaPlayer? _preview;
    private string _previewPath = "";
    private int _previewCard;
    /// <summary>
    /// XAML 里筛选框的 SelectedIndex=0 会在 InitializeComponent 期间就触发 SelectionChanged,
    /// 那时列表还没有数据, 提前跑过滤会把状态栏写成「共 0 张」。初始化完再放开。
    /// </summary>
    private bool _filterReady;

    /// <summary>窄上下按钮只在第一次 Loaded 时换一次（页面开了缓存，Loaded 会重复触发）。</summary>
    private bool _spinsDone;

    /// <summary>内容右侧给竖滚动条留的槽宽。滚动条是浮层，不留槽会压住最右侧那条边框。</summary>
    private const double ScrollGutter = 18;

    public AnimPage()
    {
        InitializeComponent();

        CardList.ItemsSource = _shown;

        // 下拉条目和两个表单的显隐都按模式生成，别指望 XAML 里写死的那一份
        ApplyMode();

        // XAML 里的 Value="4.8" 会先按 float 落成 4.800000019, 格式器又晚一步,
        // 所以这里重设格式器和值, 显示才干净。
        // 小数步进还必须带 IncrementNumberRounder: 只给 FractionDigits 时 NumberBox
        // 会先把值四舍五入成整数再加减, 上下键就走成 1 而不是 0.1。
        DurBox.NumberFormatter = DecimalFormatterFor(1, 0.1);
        DurBox.Value = 4.8;
        ZoomBox.NumberFormatter = DecimalFormatterFor(1, 0.1);
        ZoomBox.Value = 1.1;
        // 步进 0.01：内置方案的 1800 单位折合 0.625，用 0.05 会被吸附到 0.65
        SizeBox.NumberFormatter = DecimalFormatterFor(2, 0.01);
        SizeBox.Value = 1;
        FxDurBox.NumberFormatter = DecimalFormatterFor(1, 0.1);
        FxDurBox.Value = 2.0;
        FxSizeBox.NumberFormatter = DecimalFormatterFor(2, 0.05);
        FxSizeBox.Value = 0.5;

        // 数值框清空后失焦要回填，不然框里一直空着（读值有 Num() 兜底，但显示不能空）
        HookNumberBoxes();


        // 内置的上下按钮太宽（框架固定 32px），换成自己的窄按钮。
        // 必须等 Loaded：构造函数里这些框的 Parent 还是 null（XAML 树没挂上），改不了父级。
        Loaded += (_, _) =>
        {
            if (_spinsDone) return;
            _spinsDone = true;
            UseNarrowSpinners();
        };

        // 日志由 App 单点订阅（见 App.OnLaunched），这里只留进度订阅。
        if (App.Engine is { } engine)
            engine.ProgressChanged += OnEngineProgress;
        // 页面开了 NavigationCacheMode=Required, 切走再切回来是同一个实例,
        // 所以这里不随 Unloaded 退订: 否则离开期间进度就断流了。

        // ScrollViewer 在横向不约束内容宽度, 里面的 * 列会被量成无限宽导致整体溢出裁切。
        // 把内容网格的宽度钉在可视区(ActualWidth)上, 参数区就不会被挤出右边缘。
        // 注意用 ActualWidth 而不是 ViewportWidth: 不可滚动时后者返回的是内容总宽(更大), 会越绑越宽。
        // 再减掉一条滚动条槽: 竖滚动条是浮层, 不留槽就会压住内容最右侧那条边框。
        DetailScroller.SizeChanged += (_, _) =>
        {
            var vw = DetailScroller.ActualWidth;
            if (vw <= 0) return;
            var w = Math.Max(0, vw - ScrollGutter);
            if (double.IsNaN(DetailContent.Width) || Math.Abs(DetailContent.Width - w) > 1)
                DetailContent.Width = w;
        };
    }

    /// <summary>小数参数框的数字格式器: 显式位数 + 步进取整, 否则上下键会被四舍五入成整数。</summary>
    private static DecimalFormatter DecimalFormatterFor(int digits, double increment) => new()
    {
        IntegerDigits = 1,
        FractionDigits = digits,
        NumberRounder = new IncrementNumberRounder
        {
            Increment = increment,
            RoundingAlgorithm = RoundingAlgorithm.RoundHalfUp,
        },
    };

    /// <summary>
    /// 把 NumberBox 内置的上下按钮换掉。内置那两枚是框架主题字典里编译期绑定的样式，
    /// 固定 32px 宽，实测两枚加间距占掉 76px，四列参数框里太挤、把内容顶到功能块右边缘；
    /// App 级定义同名样式覆盖不到（试过，无效）。这里关掉内置按钮，在右侧补两枚
    /// 18×22 的窄按钮，横向只占 40px。
    /// </summary>
    private void UseNarrowSpinners()
    {
        foreach (var box in new[]
                 {
                     FramesBox, DurBox, CellBox, SizeBox,
                     OffXBox, OffYBox, RotBox, ZoomBox,
                     FxFramesBox, FxDurBox, FxSizeBox, FxOffXBox, FxOffYBox, FxRotBox,
                 })
        {
            if (box?.Parent is Grid parent) NarrowSpin(box, parent);
        }
    }

    private void NarrowSpin(NumberBox box, Grid parent)
    {
        int row = Grid.GetRow(box), col = Grid.GetColumn(box);
        int rowSpan = Grid.GetRowSpan(box), colSpan = Grid.GetColumnSpan(box);
        parent.Children.Remove(box);

        box.SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Hidden;

        // 数值框铺满列宽（八列等宽、左右都贴齐整块控件），两枚方形按钮叠在右端内侧，
        // 文本右侧让出按钮的位置，免得输入的字钻到按钮下面。
        box.HorizontalAlignment = HorizontalAlignment.Stretch;
        box.Padding = new Thickness(
            box.Padding.Left, box.Padding.Top, box.Padding.Right + 50, box.Padding.Bottom);

        var wrap = new Grid { HorizontalAlignment = HorizontalAlignment.Stretch };
        wrap.Children.Add(box);

        var spins = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 1,
            HorizontalAlignment = HorizontalAlignment.Right,
            // 底部留 5：NumberBox 上边是表头、下边才是输入行，按钮对齐输入行的中线
            VerticalAlignment = VerticalAlignment.Bottom,
            Margin = new Thickness(0, 0, 3, 5),
        };
        spins.Children.Add(MakeSpinButton(box, "\uE70E", +1));
        spins.Children.Add(MakeSpinButton(box, "\uE70D", -1));
        wrap.Children.Add(spins);

        // NumberBox 内层 TextBox 自带一个「清除」✕，聚焦且有内容时就冒出来，正好挤在两枚
        // 按钮中间。没有公开属性可关，只好等模板落地后把它从视觉树里按下去。
        wrap.Loaded += (_, _) => HideClearButton(box);

        Grid.SetRow(wrap, row);
        Grid.SetColumn(wrap, col);
        Grid.SetRowSpan(wrap, rowSpan);
        Grid.SetColumnSpan(wrap, colSpan);
        parent.Children.Add(wrap);
    }

    /// <summary>
    /// 收起 TextBox 模板里的清除按钮（名字就叫 DeleteButton）。
    /// 模板要等这块区域真正显示出来才落地（进页面时详情区还是折叠的），所以用个短周期定时器
    /// 找它，找到就按死。
    ///
    /// 2026-11-21：原先只把 Visibility 设回 Collapsed 再挂个回调，没用。模板里那个
    /// ButtonStates 组用的是离散关键帧动画（ButtonVisible 一直 HoldEnd），动画值盖住本地值，
    /// 所以聚焦、有内容时 × 照样冒出来，挤在两枚上下按钮中间。
    /// 改成把按钮本身压成零尺寸加全透明：动画只动 Visibility，动不了这几个属性。
    /// </summary>
    private static void HideClearButton(NumberBox box)
    {
        if (TryLatchClearButton(box)) return;

        // 模板还没落地：先挂个聚焦回调兜底（用户点进去时一定能拿到），再轮询一阵
        box.GotFocus += (_, _) => TryLatchClearButton(box);

        var timer = box.DispatcherQueue.CreateTimer();
        timer.Interval = TimeSpan.FromMilliseconds(150);
        int ticks = 0;
        timer.Tick += (_, _) =>
        {
            if (TryLatchClearButton(box) || ++ticks > 40) timer.Stop();
        };
        timer.Start();
    }

    private static bool TryLatchClearButton(NumberBox box)
    {
        if (FindByName(box, "DeleteButton") is not Button del) return false;
        del.Width = 0;
        del.MinWidth = 0;
        del.Padding = new Thickness(0);
        del.Margin = new Thickness(0);
        del.Opacity = 0;
        del.IsHitTestVisible = false;
        del.Visibility = Visibility.Collapsed;
        return true;
    }

    private static FrameworkElement? FindByName(DependencyObject root, string name)
    {
        int n = VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is FrameworkElement fe && fe.Name == name) return fe;
            if (FindByName(child, name) is { } hit) return hit;
        }
        return null;
    }

    private RepeatButton MakeSpinButton(NumberBox box, string glyph, int dir)
    {
        var btn = new RepeatButton
        {
            Style = (Style)Resources["NarrowSpinButton"],
            Content = glyph,
            IsTabStop = false,
            Delay = 400,
            Interval = 80,
        };
        btn.Click += (_, _) => Step(box, dir);
        // 引擎跑起来时会把整个框禁用，按钮跟着一起灰掉
        btn.SetBinding(Control.IsEnabledProperty, new Binding
        {
            Path = new PropertyPath(nameof(IsEnabled)),
            Source = box,
            Mode = BindingMode.OneWay,
        });
        return btn;
    }

    /// <summary>按 SmallChange 走一步并夹在上下限内；框被清空(NaN)时从下限起算。</summary>
    private static void Step(NumberBox box, int dir)
    {
        double v = box.Value;
        if (double.IsNaN(v)) v = double.IsNaN(box.Minimum) ? 0 : box.Minimum;
        v += dir * box.SmallChange;
        if (!double.IsNaN(box.Maximum) && v > box.Maximum) v = box.Maximum;
        if (!double.IsNaN(box.Minimum) && v < box.Minimum) v = box.Minimum;
        box.Value = v;
    }

    /// <summary>
    /// NumberBox 被清空后 Value 变成 NaN，WinUI 失焦时不会自动回填，框里就一直空着。
    /// 这里记住每个框上一个有效值，失焦时若为空就填回去，视觉上等于「恢复默认」。
    /// 数值读取本来就有 Num() 兜底，这段只影响显示。
    /// </summary>
    private void HookNumberBoxes()
    {
        foreach (var box in new[]
                 {
                     FramesBox, DurBox, CellBox, SizeBox,
                     OffXBox, OffYBox, RotBox, ZoomBox,
                     FxFramesBox, FxDurBox, FxSizeBox, FxOffXBox, FxOffYBox, FxRotBox,
                 })
        {
            if (box is null) continue;
            double last = box.Value;   // 每轮一个独立变量，各记各的上一个有效值
            box.ValueChanged += (_, _) =>
            {
                if (!double.IsNaN(box.Value)) last = box.Value;
            };
            box.LostFocus += (_, _) =>
            {
                if (double.IsNaN(box.Value)) box.Value = last;
                // NumberBox 有时在 LostFocus 之后才把空文本落成 NaN，延后一拍再兜一次
                box.DispatcherQueue.TryEnqueue(() =>
                {
                    if (double.IsNaN(box.Value)) box.Value = last;
                });
            };
        }
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        if (App.Engine is null)
        {
            ShowFatal("引擎没有启动：" + (App.EngineError ?? "未知原因"));
            return;
        }
        // 任务进行中切回本页不重载：任务结束时 RunJobAsync 会带卡号重载一次，这里的
        // 重载只会把列表、源卡预设、计数文字推回半截状态，还会抢走任务的状态条
        // （2026-11-12 用户实测）。首次进页（还没有数据）照旧加载。
        if ((_jobActive || App.MainBusy) && _all.Count > 0) return;

        // 无条件重载：撤销/复原可能发生在别的页面（分享页恢复原版、各页复原），
        // 这里的列表快照（分类标签、复原按钮状态）会过期。cards.status 是轻查询，
        // 切页重算一次换状态永远新鲜，值得。
        await ReloadAsync();
    }

    // ------------------------------------------------------------- 数据加载
    /// <param name="focusId">
    /// 操作完成后要落回去的卡号。改一张卡的特效或动画会让它的标签变化
    /// （官方 → 自改、自改 → 官方），而筛选下拉还停在操作前那一档，
    /// 这张卡随即离开列表，看起来像被操作弄丢了。传卡号进来，重载后把它找回来。
    /// 0 = 不聚焦，走原来的兜底选中（刷新按钮那条路还要手动清掉选中）。
    /// </param>
    private async Task ReloadAsync(int focusId = 0)
    {
        if (App.Engine is not { } engine) return;
        // 刷新意味着游戏文件可能被别的途径改过，音效状态不能再拿缓存
        _soundCache.Clear();
        SetBusy(true, "正在读取卡片状态…");
        try
        {
            var data = await engine.CallAsync("cards.status");
            (_previewCutin, _previewRunfx) = await LoadPreviewCardsAsync(engine);
            _all = ParseCards(data, _previewCutin, _previewRunfx);
            foreach (var c in _all) c.Mode = _mode;
            await LoadDonorPresetsAsync(engine);
            FillDonorCandidates();
            await LoadSchemesAsync(engine);

            // 重载会重建全部 CardItem，旧的 _current 引用随之作废；_shown.Clear()
            // 还会触发一次 SelectionChanged，先把右侧表单清空、再落到第一条，
            // 用户看到的就是「改完一张，右边突然变成别的卡」。整段按住选中回调，
            // 收尾时一次性落到该落的卡上，只刷一次详情。
            _revertingSelection = true;
            try
            {
                _current = null;
                await ApplyFilterAsync();
                if (focusId > 0 && _all.FirstOrDefault(c => c.Id == focusId) is { } target)
                    await FocusCardAsync(target);
                else
                    KeepSelection();
                _current = CardList.SelectedItem as CardItem;
                ShowDetail(_current);
                // 筛选带防抖与取消：被取消时收尾的赋值不会执行，计数就空着。
                // 这里兜一句，列表里有内容时底下不能没有字。
                if (string.IsNullOrEmpty(CountText.Text) && _shown.Count > 0)
                    CountText.Text = $"共 {_shown.Count} 张{FilterSuffix()}";
            }
            finally
            {
                _revertingSelection = false;
            }
        }
        catch (Exception ex)
        {
            SetStatus("读取失败：" + ex.Message);
        }
        finally
        {
            SetBusy(false, null);
        }
    }

    /// <summary>
    /// 重载后把焦点放回刚改的那张卡：标签换了档位就跟过去，然后选中并滚到可见。
    ///
    /// 只跟档、不改搜索词。搜索框是用户显式输入的状态，替人清掉会断掉他的上下文；
    /// 真出现「切了档还找不到」，那是后端搜索结果 200 条上限截断，只说明事实。
    /// </summary>
    private async Task FocusCardAsync(CardItem target)
    {
        // 这张卡在新数据里落在哪一档：official / shared / self / selfOfficial / none
        var want = KindOfCurrent(target);
        if (want.Length > 0 && want != StatusFilter && ShowsInMode(target))
        {
            for (var i = 0; i < StatusFilterBox.Items.Count; i++)
            {
                if ((StatusFilterBox.Items[i] as ComboBoxItem)?.Tag as string != want) continue;
                // 换档会触发 SelectionChanged 再筛一遍，先按住，下面自己 await 一次，
                // 免得两次过滤并发、列表被重建两遍。
                _filterReady = false;
                StatusFilterBox.SelectedIndex = i;
                _filterReady = true;
                await ApplyFilterAsync();
                break;
            }
        }

        // 不能用引用比: 有搜索词时列表是后端返回的、ParseCards 重建的新对象，
        // 和 _all 里那批不是同一个实例。按卡号找。
        var hit = _shown.FirstOrDefault(c => c.Id == target.Id);
        if (hit is not null)
        {
            CardList.SelectedItem = hit;
            // 数据刚换过，容器还没生成，直接 ScrollIntoView 算不出位置。先强制布局。
            CardList.UpdateLayout();
            CardList.ScrollIntoView(hit);
            return;
        }

        CountText.Text = $"共 {_shown.Count} 张{FilterSuffix()} · 这张卡不在当前搜索结果内";
    }

    /// <summary>
    /// 本地有预览缓存的卡号，动画与特效分开。查不到就返回 null：宁可留着播放入口
    /// 让用户点一下，也不要把所有卡的入口都藏起来。
    /// </summary>
    private static async Task<(HashSet<int>?, HashSet<int>?)> LoadPreviewCardsAsync(EngineClient engine)
    {
        try
        {
            var rows = await engine.CallAsync("preview.list");
            if (rows.ValueKind != JsonValueKind.Array) return (null, null);
            var cutin = new HashSet<int>();
            var runfx = new HashSet<int>();
            foreach (var r in rows.EnumerateArray())
            {
                if (!r.TryGetProperty("card", out var c) || c.ValueKind != JsonValueKind.Number) continue;
                var kind = r.TryGetProperty("kind", out var k) && k.ValueKind == JsonValueKind.String
                    ? k.GetString() : "cutin";
                (kind == "runfx" ? runfx : cutin).Add(c.GetInt32());
            }
            return (cutin, runfx);
        }
        catch
        {
            return (null, null);
        }
    }

    private static List<CardItem> ParseCards(JsonElement data, HashSet<int>? cutinPreviews,
                                             HashSet<int>? runfxPreviews)
    {
        var list = new List<CardItem>();
        if (data.ValueKind != JsonValueKind.Object ||
            !data.TryGetProperty("cards", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return list;

        foreach (var c in arr.EnumerateArray())
        {
            // 占位副本号（同名同图、游戏不渲染的那个号）直接不列：写过去也不会有反应
            if (c.TryGetProperty("real", out var r) && r.ValueKind == JsonValueKind.Number
                && r.GetInt32() > 0)
                continue;
            int cardId = c.TryGetProperty("id", out var idEl) ? idEl.GetInt32() : 0;
            var item = new CardItem
            {
                Id = cardId,
                HasCutinPreview = cutinPreviews is null || cutinPreviews.Contains(cardId),
                HasRunfxPreview = runfxPreviews is null || runfxPreviews.Contains(cardId),
                Name = Str(c, "name"),
                Cutin = Str(c, "cutin"),
                Runfx = Str(c, "runfx"),
                Kind = Str(c, "kind"),
                IsPlain = c.TryGetProperty("plain", out var pl) && pl.ValueKind == JsonValueKind.True,
                Unreleased = c.TryGetProperty("unreleased", out var ur) && ur.ValueKind == JsonValueKind.True,
                UnknownSubtype = c.TryGetProperty("unknown_subtype", out var us) && us.ValueKind == JsonValueKind.True,
                Locked = c.TryGetProperty("locked", out var lk) && lk.ValueKind == JsonValueKind.True,
                SubtypeCn = Str(c, "subtype_cn"),
                CoarseCn = Str(c, "coarse_cn"),
                SpecialNote = c.TryGetProperty("special", out var sp) && sp.ValueKind == JsonValueKind.String
                    ? sp.GetString() ?? "" : "",
                // 来源卡：后端从 cutin.DONORS 算出来，界面标徽章并禁用生成/导入按钮
                IsDonor = c.TryGetProperty("donor", out var dn) && dn.ValueKind == JsonValueKind.True,
                CutinSrc = c.TryGetProperty("src", out var src) && src.ValueKind == JsonValueKind.Number
                    ? src.GetInt32() : 0,
                CutinSrcName = Str(c, "src_name"),
                RunfxSrc = c.TryGetProperty("fx_src", out var fs) && fs.ValueKind == JsonValueKind.Number
                    ? fs.GetInt32() : 0,
                RunfxSrcName = Str(c, "fx_src_name"),
                FxOrigin = Str(c, "fx_origin"),
                CutinOrigin = Str(c, "cutin_origin"),
            };
            // 动手前官方本来就有特效：不进标签，只用来把状态行换成「复原可以还原」那套说法。
            // 具体是自有还是借用，看 FxOrigin（自改那行的悬停据此写「来自官方特效(★)」）。
            item.FxFromOfficial =
                c.TryGetProperty("fx_from_official", out var ff) && ff.ValueKind == JsonValueKind.True;
            list.Add(item);
        }
        return list;
    }

    private static string Str(JsonElement obj, string key)
        => obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    // --------------------------------------------------------------- 过滤
    // 搜索走后端: 后端查的是完整卡名表(14257 张), 本地只有本工具动过的那几张,
    // 搜"耀圣"这种能匹配很多卡的词只出得来几张.
    private CancellationTokenSource? _searchCts;

    private void SearchBox_TextChanged(object sender, TextChangedEventArgs e) => _ = ApplyFilterAsync();

    private void FilterBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_filterReady) return;
        _ = ApplyFilterAsync();
    }

    /// <summary>当前筛选值。空串=不限，其余是 official / self / none，和卡片标签同一套。</summary>
    private static string TagOf(ComboBox box)
        => (box.SelectedItem as ComboBoxItem)?.Tag as string ?? "";

    /// <summary>
    /// 当前模式：`cutin` = 改召唤动画，`runfx` = 改发动特效。
    ///
    /// 这一维不只决定右边显示哪个表单，也决定左边能搜到哪些卡：召唤动画只存在于怪兽卡上，
    /// 魔法陷阱卡再多也只有发动特效。所以这类卡压根不该出现在召唤动画的搜索里，
    /// 而不是出现了再标一句「无动画」。
    /// </summary>
    private string _mode = "cutin";

    private bool InCutinMode => _mode == "cutin";

    /// <summary>当前筛选值。空串=不限，其余是 official / shared / self / selfOfficial / none。</summary>
    private string StatusFilter => TagOf(StatusFilterBox);

    /// <summary>模式变了: 换下拉条目、换右侧表单、重跑一遍过滤。</summary>
    private async void ApplyMode()
    {
        // 下拉里放这几档，跟列表行上的标签一一对应。
        // 两页都是四档：官方 / 官方★（借用同名卡）/ 自改 / 无。自改一律不带★，
        // 原本有没有官方那套属于来历，写在悬停里，不占一档。
        // 「无」放末尾：前几档都是「有东西」的类别，先按来路分，最后才是没有。
        var items = InCutinMode
            ? new[] { ("官方动画", "official"), ("官方动画★", "shared"),
                      ("自改动画", "self"), ("无动画", "none") }
            : new[] { ("官方特效", "official"), ("官方特效★", "shared"),
                      ("自改特效", "self"), ("无特效", "none") };

        // 换条目会触发 SelectionChanged，先按住
        _filterReady = false;
        StatusFilterBox.Items.Clear();
        foreach (var (text, tag) in items)
            StatusFilterBox.Items.Add(new ComboBoxItem { Content = text, Tag = tag });
        StatusFilterBox.SelectedIndex = 0;
        _filterReady = true;

        CutinSection.Visibility = InCutinMode ? Visibility.Visible : Visibility.Collapsed;
        RunfxSection.Visibility = InCutinMode ? Visibility.Collapsed : Visibility.Visible;

        // 列表行只显示当前模式那一维的标签
        foreach (var c in _all) c.Mode = _mode;
        foreach (var c in _shown) c.Mode = _mode;

        // 标题行那两枚按钮和说明文字是两模式共用的，判据却分两维。
        // 切模式不一定重跑 ShowDetail（选中项没变就不会触发 SelectionChanged），
        // 所以这里显式翻一次，否则切到特效页还显示动画那套说明、复原按钮按动画的状态亮着。
        if (_current is { } cur)
        {
            ShowHeader(cur);
            RestoreButton.IsEnabled = InCutinMode ? cur.HasOurs : cur.HasOursRunfx;
            SetStateLine(cur);
        }
        // 两个模式的内容高度不同，沿用上一页的滚动位置会让标题被顶出可视区
        DetailScroller.ChangeView(null, 0, null, true);

        // 构造函数里会先跑一次，那时数据还没到，别把状态栏改写成「共 0 张」
        if (_all.Count == 0) return;

        await ApplyFilterAsync();
        // 切过来时原来选中的卡可能不在新列表里（动画卡是通常怪兽就没有特效），
        // 那样右侧会一片空，必须先点一下左边才出内容。这里补一次自动选中。
        if (CardList.SelectedItem is null || _current is null)
        {
            KeepSelection();
            _current = CardList.SelectedItem as CardItem;
            ShowDetail(_current);
        }
    }

    /// <summary>当前模式下的筛选类别，取 official / shared / self / none，跟下拉 Tag 同一套词。</summary>
    private string KindOfCurrent(CardItem c) => InCutinMode ? c.FilterCutin : c.FilterRunfx;

    /// <summary>
    /// 这张卡在当前模式下该不该出现在列表里。
    /// 召唤动画只给怪兽卡；发动特效反过来要排掉通常怪兽和衍生物，它们没有可改的特效。
    /// </summary>
    private bool ShowsInMode(CardItem c) => InCutinMode ? c.InCutinList : !c.IsPlain;

    /// <summary>状态栏里的筛选说明。下拉有五档、永远有选中项，直接把当前范围写出来。</summary>
    private string FilterSuffix()
    {
        var text = (StatusFilterBox.SelectedItem as ComboBoxItem)?.Content as string ?? "";
        return text.Length > 0 ? "（" + text + "）" : "";
    }

    /// <summary>列表空了要给句话，否则一片空白看起来像卡住了。</summary>
    private void UpdateEmptyHint()
        => EmptyHint.Visibility = _shown.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

    private async Task ApplyFilterAsync()
    {
        var q = SearchBox.Text.Trim();
        var status = StatusFilter;
        // 「无动画 / 无特效」是全表补集，本地 _all 只装了门禁表里那几百张有资源的卡，
        // 光靠它筛永远只有十几张。这一档必须问后端，其余档位本地筛更快。
        if (q.Length == 0 && status != "none")
        {
            _shown.Clear();
            foreach (var c in _all)
            {
                if (!ShowsInMode(c)) continue;
                if (status.Length > 0 && KindOfCurrent(c) != status) continue;
                _shown.Add(c);
            }
            UpdateEmptyHint();
            SetBusy(false, null);
            StatusStrip.Visibility = Visibility.Collapsed;
            CountText.Text = $"共 {_shown.Count} 张{FilterSuffix()}";
            KeepSelection();
            return;
        }

        // 防抖: 打字时别每敲一下就查一次
        _searchCts?.Cancel();
        var cts = new CancellationTokenSource();
        _searchCts = cts;
        try
        {
            await Task.Delay(250, cts.Token);
        }
        catch (OperationCanceledException)
        {
            return;
        }

        if (App.Engine is not { } engine) return;
        SetBusy(true, q.Length > 0 ? $"正在搜「{q}」…" : "正在读取卡片状态…");
        try
        {
            var args = new Dictionary<string, object?> { ["q"] = q, ["limit"] = SearchLimit };
            // 筛选交给后端: 只有后端知道匹配的全集, 前端筛会把 limit 之外的结果漏掉
            if (InCutinMode) args["kind"] = "monster";
            else args["no_plain"] = true;
            if (status.Length > 0)
                args[InCutinMode ? "cutin" : "runfx"] = status;
            var data = await engine.CallAsync("cards.search", args);
            if (cts.IsCancellationRequested) return;

            var rows = ParseCards(data, _previewCutin, _previewRunfx);
            _shown.Clear();
            foreach (var c in rows) { c.Mode = _mode; _shown.Add(c); }
            UpdateEmptyHint();
            SetBusy(false, null);
            StatusStrip.Visibility = Visibility.Collapsed;
            // count 是后端过滤后、截断前的总数。「无动画」有八千多张，列表只取前
            // 200 条，写成 200 会让人以为全游戏只有 200 张，跟卡图页「无超框」一致处理。
            var total = data.ValueKind == JsonValueKind.Object
                        && data.TryGetProperty("count", out var cv)
                        && cv.ValueKind == JsonValueKind.Number
                ? cv.GetInt32() : _shown.Count;
            var suffix = FilterSuffix();
            if (total > _shown.Count) suffix += $" · 最多列 {SearchLimit} 张";
            CountText.Text = q.Length == 0
                ? $"共 {total} 张{suffix}"
                : $"「{q}」找到 {total} 张{suffix}";
            KeepSelection();
        }
        catch (Exception ex)
        {
            SetBusy(false, "搜索失败：" + ex.Message);
        }
    }

    /// <summary>刷新后尽量留在原来选中的那张, 没有就选第一条。</summary>
    private void KeepSelection()
    {
        var keep = _current;
        // 特殊卡和锁定的卡都点不了，别自动选上（否则会停在一个灰行上）
        if (keep is not null && !keep.IsSpecial && !keep.Locked && _shown.Contains(keep))
        {
            CardList.SelectedItem = keep;
            return;
        }
        var first = _shown.FirstOrDefault(c => !c.IsSpecial && !c.Locked);
        if (first is not null)
        {
            CardList.SelectedItem = first;
        }
        else
        {
            _current = null;
            CardList.SelectedItem = null;
            ShowDetail(null);
        }
    }

    // --------------------------------------------------------------- 选中
    /// <summary>
    /// 特殊卡灰掉、点不动。跟卡图页的占位卡同一套做法。
    ///
    /// 超融合、召唤魔术这批卡播的是融合召唤演出，游戏里没有它们的发动特效条目，
    /// 改了也不会播。让用户能选中并填半天表单，最后白做。
    /// </summary>
    private void CardList_ContainerContentChanging(ListViewBase sender, ContainerContentChangingEventArgs args)
    {
        if (args.ItemContainer is not ListViewItem box || args.Item is not CardItem card) return;
        // 容器会被回收复用，两个方向都要显式设回去。锁定与特殊卡共用灰态，
        // 但含义不同：特殊卡是这一维不适用，锁定是暂时不能改。
        bool dim = card.IsSpecial || card.Locked;
        box.IsEnabled = !dim;
        box.Opacity = dim ? 0.45 : 1.0;
    }

    private void CardList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_revertingSelection) return;
        // 生成过程中不许换卡: 把选中项拨回去, 右侧详情保持不动
        if (_jobRunning)
        {
            if (!ReferenceEquals(CardList.SelectedItem, _current))
            {
                _revertingSelection = true;
                CardList.SelectedItem = _current;
                _revertingSelection = false;
            }
            return;
        }
        // 灰掉之后本来就点不到，兜一层防止别处（键盘、KeepSelection）把它选中
        if (CardList.SelectedItem is CardItem special && (special.IsSpecial || special.Locked))
        {
            _revertingSelection = true;
            CardList.SelectedItem = _current;
            _revertingSelection = false;
            return;
        }
        _current = CardList.SelectedItem as CardItem;
        ShowDetail(_current);
    }

    /// <summary>卡名与卡号卡型。切模式时也要重刷一次，否则会出现只有内容没有标题的一屏</summary>
    private void ShowHeader(CardItem card)
    {
        CardName.Text = card.Title;
        // 状态标签在左边列表里显示，这里留卡号和卡型
        CardMeta.Text = string.IsNullOrEmpty(card.SubtypeCn)
            ? card.IdText : $"{card.IdText} · {card.SubtypeCn}";
    }

    private void ShowDetail(CardItem? card)
    {
        if (card is null)
        {
            EmptyState.Visibility = Visibility.Visible;
            DetailPanel.Visibility = Visibility.Collapsed;
            return;
        }

        EmptyState.Visibility = Visibility.Collapsed;
        DetailPanel.Visibility = Visibility.Visible;

        ShowHeader(card);
        MaybeShowCarryHint(card);

        // 刷新 / 复原现在两个模式共用同一套控件，启用状态取当前模式那一维：
        // 动画模式看 HasOurs，特效模式看 HasOursRunfx。
        RestoreButton.IsEnabled = InCutinMode ? card.HasOurs : card.HasOursRunfx;
        SetStateLine(card);

        // 三档都用来源卡：官方动画、官方★、无动画。2026-11-22 起视频生成一律克隆来源卡，
        // 官方那套的 prefab、相机、Timeline 分镜会一起换成来源卡的。
        // 自改动画（yours / wiped）是已经生成过的，来源卡不再参与，藏起来。
        var showDonor = card.Cutin is not "yours" and not "wiped";
        DonorPanel.Visibility = showDonor ? Visibility.Visible : Visibility.Collapsed;

        // 已经用本工具改过的卡，生成区整块收起来：必须先复原才能再生成，
        // 留着输入框和生成按钮只会让人点了才看到「已经挂着动画了」。
        // 特效那边只收「用视频自制」，音效区照旧——换声音不需要重做一套动画。
        var cutinOurs = !showDonor;
        CutinBuildPanel.Visibility = cutinOurs ? Visibility.Collapsed : Visibility.Visible;
        SpineImportPanel.Visibility = CutinBuildPanel.Visibility;
        RunfxBuildPanel.Visibility = card.Runfx is "yours" or "wiped"
            ? Visibility.Collapsed : Visibility.Visible;
        DonorHint.Text = "按召唤类型自动选定，可换成别的预设。整套动画结构复制给这张卡，来源卡不受影响。";
        // 面板藏起来也要把值填上：后端 apply 第一步就要求 donor 非空，官方卡虽然不拿它
        // 当底盘，缺了这个参数会直接报「先选一张来源卡」。
        // 下拉在重载时被重建过（Items.Clear 会清掉选中态），按框里现在这张补回来，
        // 否则会出现「来源卡是 11358、预设却显示按类型选」（2026-11-12 用户实测）。
        SyncDonorPreset(ReadDonorId(DonorBox) ?? -1);
        _ = FillDonorAsync(card);

        // 来源卡：后端在视频生成、Spine 导入、换贴图三个入口都硬拦，界面把按钮一起锁掉，
        // 别让用户点一次再看到报错。
        ApplyDonorLock(card);

        // 音效跟着当前卡走
        UpdateSoundUi();
        _ = RefreshSoundAsync();
        // 预设时长首次要跑三次 ffmpeg（约 1.2 秒）。趁切卡在后台预热，
        // 免得用户点开弹窗时干等；之后走索引缓存只要 3 毫秒。
        if (!_presetsLoaded) _ = LoadPresetsAsync(true);
    }

    /// <summary>
    /// 来源卡不允许改召唤动画。
    ///
    /// 它是无动画卡的克隆底盘：页数和材质槽会被后来克隆的卡继承，改过之后那些卡会跟着
    /// 漂到不匹配的结构。后端在视频生成、Spine 导入、换贴图三处硬拦，这里只是把按钮
    /// 一起锁掉，免得用户白点一次。复原不受这条限制。
    /// </summary>
    private void ApplyDonorLock(CardItem card)
    {
        _donorLocked = card.IsDonor;
        if (_jobActive) return;   // 任务在跑时按钮由 SetRunning 统一管，收尾会再调回来
        BuildButton.IsEnabled = !_donorLocked;
        SpineImportButton.IsEnabled = !_donorLocked;
        BuildButton.Content = _donorLocked ? "来源卡不可修改" : "生成召唤动画";
        SpineImportButton.Content = _donorLocked ? "来源卡不可修改" : "导入到这张卡";
    }

    /// <summary>
    /// 标题下面那行状态说明。四种状态各写一句，句式统一成「这张卡…」，
    /// 这样在列表里来回切卡时，这一行的位置和语气都不变，只有内容在换。
    /// 动画与特效两个模式共用这一个控件。
    /// </summary>
    /// <summary>
    /// 换一张卡时提一句「参数还是上一张的」。只在这一屏待过别的卡、且当前参数已偏离
    /// 默认值时才出现，默认值状态不打扰（2026-11-23 加）。
    /// </summary>
    private void MaybeShowCarryHint(CardItem card)
    {
        var prevId = _lastShownId;
        var prevMode = _lastShownMode;
        _lastShownId = card.Id;
        _lastShownMode = _mode;

        CutinCarryHint.Visibility = Visibility.Collapsed;
        FxCarryHint.Visibility = Visibility.Collapsed;
        _carryTarget = null;

        if (prevId == 0 || prevId == card.Id || prevMode != _mode) return;
        if (!HasTunedParams()) return;

        // 提示挂在每个模式各自那一行的右端：动画在「来源卡」这行，特效在「时机」这行。
        // 那两行在「自改动画」状态下会收起来，没地方挂就不提，不另找位置挤排版。
        if (InCutinMode)
        {
            if (card.Cutin is "yours" or "wiped") return;
            _carryTarget = CutinCarryHint;
        }
        else
        {
            _carryTarget = FxCarryHint;
        }

        _carryTarget.Visibility = Visibility.Visible;
        _carryTimer ??= CreateCarryTimer();
        _carryTimer.Start();
    }

    private Microsoft.UI.Dispatching.DispatcherQueueTimer CreateCarryTimer()
    {
        var t = DispatcherQueue.CreateTimer();
        t.Interval = TimeSpan.FromSeconds(4);
        t.IsRepeating = false;
        t.Tick += (_, _) =>
        {
            if (_carryTarget is { } target) target.Visibility = Visibility.Collapsed;
        };
        return t;
    }

    /// <summary>参数是否已经偏离默认值。全是默认值时，沿用不沿用没有区别，不必提示。</summary>
    private bool HasTunedParams()
    {
        static bool Diff(double a, double b) => Math.Abs(a - b) > 1e-6;

        if (InCutinMode)
            return Diff(Num(FramesBox, 120), 120) || Diff(Num(DurBox, 4.8), 4.8)
                || Diff(Num(CellBox, 1664), 1664) || Diff(Num(SizeBox, 1), 1)
                || Diff(Num(OffXBox, 0), 0) || Diff(Num(OffYBox, 0), 0)
                || Diff(Num(RotBox, 0), 0) || Diff(Num(ZoomBox, 1.1), 1.1);

        return Diff(Num(FxFramesBox, 60), 60) || Diff(Num(FxDurBox, 2.0), 2.0)
            || Diff(Num(FxSizeBox, 0.5), 0.5) || Diff(Num(FxOffXBox, 0), 0)
            || Diff(Num(FxOffYBox, 0), 0) || Diff(Num(FxRotBox, 0), 0);
    }

    private void SetStateLine(CardItem card)
    {
        StateLine.Text = StateLineOf(card);
        // 少数卡（#14298 那类）的官方演出本身就是特效，光球、火焰、光环都是骨架里
        // 额外挂的槽，换成序列帧后这些槽不存在，观感和原版会差很多。只在官方动画
        // 这一档挂提示，别的状态不打扰。
        ToolTipService.SetToolTip(StateLine, card.Cutin == "game"
            ? "少数卡的官方演出本身是特效（光柱、火焰、光环），换成序列帧后观感会和原版不同。"
            : null);
    }

    private string StateLineOf(CardItem card)
    {
        if (InCutinMode)
        {
            // 来源卡优先说明：它的动画本来就不许改，再写「可以覆盖」会误导
            if (card.IsDonor)
                return "这张卡是来源卡，其它卡的克隆底盘都用它，不能改它的召唤动画。";
            if (card.HasOurs)
                return card.CutinOrigin switch
                {
                    "shared" => "这张卡用的是你自己做的动画，原本播的是借用同名卡那套，点「复原」可以还原。",
                    "official" => "这张卡用的是你自己做的动画，官方那套已被覆盖，点「复原」可以还原。",
                    _ => "这张卡用的是工具生成的动画，点「复原」回到原样。",
                };
            if (card.CutinSrc > 0)
                return "这张卡现在播的是借来的动画，生成会用选定的来源卡单独建一套。";
            if (card.Cutin.Length > 0)
                return "这张卡自带官方召唤动画。生成会整套换成来源卡的演出，镜头与分镜跟着换，再写入你的帧。";
            return "这张卡还没有召唤动画，选定来源卡后生成一套。";
        }

        if (card.Runfx == "borrowed")
            return card.RunfxSrc > 0 && card.RunfxSrcName.Length > 0
                ? $"这张卡播的是借来的发动特效（#{card.RunfxSrc} {card.RunfxSrcName}），点「复原」回到原样。"
                : "这张卡播的是借来的发动特效，点「复原」回到原样。";
        if (card.HasOursRunfx)
            return card.FxFromOfficial
                ? "这张卡用的是你自己做的特效，官方那套已被覆盖，点「复原」可以还原。"
                : "这张卡用的是工具生成的发动特效，点「复原」回到原样。";
        if (card.Runfx == "shared")
            return "这张卡现在播的是借用同名卡的官方特效，生成会换成它自己那套。";
        if (card.Runfx == "game")
            return "这张卡自带官方发动特效，生成会覆盖它。";
        return "这张卡还没有发动特效，可以生成一套。";
    }

    // ------------------------------------------------------------------- 音效
    /// <summary>
    /// 读音效槽表。这张表跟卡片无关，整个会话只需读一次。
    /// 以前每次切换卡片都重跑一遍 IPC 并重建下拉框，点一下要等一截，就是这个原因。
    /// force=true 时强制重读（换完音效后槽的状态会变）。
    /// </summary>
    /// <summary>
    /// 读音效槽表。这张表跟卡片无关，整个会话只需读一次。
    /// </summary>

    /// <summary>每帧高度下拉对应的图集每帧像素高度。名字用纯数字，不再跟屏幕分辨率撞词</summary>
    private int FxCellHeight()
    {
        if (FxQualityBox.SelectedItem is ComboBoxItem item && item.Tag is string t && int.TryParse(t, out var v))
            return v;
        return 1080;
    }

    // --------------------------------------------------------------- 音效
    // 状态一律以引擎返回的 runfx.timings 为准。每张卡缓存一份：
    // 会话内第一次查要一秒左右（定位游戏数据 + 载入登记表），之后 3 毫秒。
    private readonly Dictionary<int, FxSoundState> _soundCache = new();
    private SoundPickRow? _officialPick;
    private SoundPickRow? _presetPick;
    // 本次会话导入的那个音频。只有一个槽位：再导入就是替换，
    // 不是攒一串历史（原来用 List 攒，界面上出现好几条同名「本地文件」）
    private SoundPickRow? _localFile;

    private async Task RefreshSoundAsync(bool force = false)
    {
        if (!force && _slotsLoaded) return;
        if (App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("sound.slots");
            _slots.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("slots", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var s in arr.EnumerateArray())
                {
                    var cards = new List<int>();
                    if (s.TryGetProperty("cards", out var cs) && cs.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var c in cs.EnumerateArray())
                            if (c.ValueKind == JsonValueKind.Number) cards.Add(c.GetInt32());
                    }
                    _slots.Add(new SoundSlot
                    {
                        Label = Str(s, "label"),
                        OnDisk = s.TryGetProperty("on_disk", out var od) && od.ValueKind == JsonValueKind.True,
                        Builtin = s.TryGetProperty("builtin", out var bi) && bi.ValueKind == JsonValueKind.True,
                        Name = Str(s, "name"),
                        Cards = cards,
                    });
                }
            }
            _slotsLoaded = true;
            UpdateSoundUi();
        }
        catch (Exception)
        {
            // 音效读不出来不影响别的功能；下次切卡会再试
        }
    }

    /// <summary>预设音效。内置的随包、用户存的在 data 目录，两边都只是一段音频文件</summary>
    private async Task LoadPresetsAsync(bool reload)
    {
        if (!reload || App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("sound.presets");
            _presets.Clear();
            if (data.TryGetProperty("presets", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in arr.EnumerateArray())
                {
                    var name = Str(p, "name");
                    if (string.IsNullOrEmpty(name)) continue;
                    _presets.Add(new PresetSound
                    {
                        Name = name,
                        Path = Str(p, "path"),
                        Builtin = p.TryGetProperty("builtin", out var bi) && bi.ValueKind == JsonValueKind.True,
                        Seconds = p.TryGetProperty("seconds", out var sc) && sc.ValueKind == JsonValueKind.Number
                            ? sc.GetDouble() : 0,
                    });
                }
            }
            _presetsLoaded = true;
        }
        catch (Exception)
        {
            _presetsLoaded = false;
        }
    }

    private async void UpdateSoundUi()
    {
        if (_current is not { } card) return;
        int id = card.Id;
        if (_soundCache.TryGetValue(id, out var cached))
        {
            ApplySoundUi(cached);
            return;
        }
        // 读之前先把这一块按「读取中」处理，否则切卡的那一秒还挂着上一张卡的文字
        FxSoundStatus.Text = "读取中…";
        FxSoundBody.IsEnabled = false;
        FxSoundRestoreButton.IsEnabled = false;

        var st = await ReadSoundStateAsync(id);
        // 这一秒里可能已经切到别的卡了，旧结果一律丢掉，否则会把上一张的状态画到这一张上
        if (_current?.Id != id) return;
        if (st is null)
        {
            FxSoundStatus.Text = "读取失败";
            FxSoundHintText.Text = "没能读到这张卡的音效状态。点右上「刷新」再试一次。";
            return;
        }
        // 引擎偶尔会在卡片索引还没建好时给出空结果，模型上明明有官方特效就再问一次
        if (!st.HasEntry && card.HasGameRunfx && App.Engine is { } engine)
        {
            await Task.Delay(300);
            if (_current?.Id != id) return;
            st = await ReadSoundStateAsync(id);
            if (_current?.Id != id) return;
            if (st is null)
            {
                FxSoundStatus.Text = "读取失败";
                FxSoundHintText.Text = "没能读到这张卡的音效状态。点右上「刷新」再试一次。";
                return;
            }
        }
        _soundCache[id] = st;
        ApplySoundUi(st);
    }

    /// <summary>任何一次写入都会改变状态，缓存跟着失效</summary>
    private void DropSoundCache()
    {
        if (_current is { } card) _soundCache.Remove(card.Id);
    }

    private void ApplySoundUi(FxSoundState? st)
    {
        if (_current is null || st is null)
        {
            FxSoundBody.IsEnabled = false;
            FxSoundRestoreButton.IsEnabled = false;
            FxSoundStatus.Text = "";
            return;
        }
        // 没有特效条目的卡, 音效无处附着, 整块不能点
        bool ok = st.HasEntry;
        FxSoundBody.IsEnabled = ok;
        FxSoundRestoreButton.IsEnabled = ok && st.CanRestore;

        // 换卡就停掉上一张的试听，别让声音跟着切卡继续放
        int cardId = _current.Id;
        if (_previewCard != cardId)
        {
            StopPreview();
            _previewPath = "";
            _previewCard = cardId;
            _officialPick = null;
            _presetPick = null;
        }

        FxOriginText.Text = DescribeOrigin(st.OriginAll
            .Where(x => !string.IsNullOrEmpty(x)).Distinct().ToList());
        FillTimings(st);
        UpdateOfficialButtons();
        UpdatePresetButtons();

        FxSoundHintText.Text = !ok
            ? "这张卡还没有发动特效，先生成特效才能配音效。"
            : "改动立即写入游戏文件。「复原音效」只还原声音，右上角「复原」整条回官方。";
        ShowSoundStatus(st, ok);
    }

    /// <summary>
    /// 标题右侧的状态。原来靠三个单选按钮表示「当前是什么」，但那表达的是意图；
    /// 现在直接写实际指向：无音效 / 官方音效：X / 自改音效：X
    /// </summary>
    private void ShowSoundStatus(FxSoundState st, bool ok)
    {
        if (FxSoundStatus is null) return;
        var cur = st.CurrentAll.FirstOrDefault(x => !string.IsNullOrEmpty(x)) ?? "";
        if (!ok || string.IsNullOrEmpty(cur))
        {
            FxSoundStatus.Text = "无音效";
            FxSoundStatus.Foreground = ThemeBrush("TextFillColorSecondaryBrush");
        }
        else if (st.Custom || cur.StartsWith("SE_EV_CUSTOM", StringComparison.Ordinal))
        {
            var name = string.IsNullOrEmpty(st.Source) ? "" : $"：{st.Source}";
            FxSoundStatus.Text = $"自改音效{name}";
            FxSoundStatus.Foreground = ThemeBrush("AccentTextFillColorPrimaryBrush");
        }
        else
        {
            FxSoundStatus.Text = $"官方音效：{SlotName(cur)}";
            FxSoundStatus.Foreground = ThemeBrush("TextFillColorSecondaryBrush");
        }
    }

    private static Brush ThemeBrush(string key) =>
        Application.Current.Resources.TryGetValue(key, out var v) && v is Brush b
            ? b
            : new SolidColorBrush(Microsoft.UI.Colors.Transparent);

    private string DescribeOrigin(List<string> origins)
    {
        if (origins.Count == 0) return "本卡原音：无（这张卡原本没有音效）";
        if (origins.Count == 1) return $"本卡原音：{SlotName(origins[0])}";
        return $"本卡原音：{origins.Count} 段不同音效，复原会按原样还原";
    }

    private string SlotName(string label)
    {
        var s = _slots.FirstOrDefault(x => x.Label == label);
        return s is null || string.IsNullOrEmpty(s.CardText) ? label : s.CardText;
    }

    /// <summary>取 label 最后一段当下标后缀，太长就不用（ASH_BLOSSOM 这种没有区分度）</summary>
    private static string LabelTail(string label)
    {
        var i = label.LastIndexOf('_');
        if (i < 0 || i == label.Length - 1) return "";
        var tail = label[(i + 1)..];
        return tail.Length <= 4 ? tail : "";
    }

    /// <summary>时机分页: 有特效按游戏已配的时机, 无特效只给一页</summary>
    private void FillTimings(FxSoundState st)
    {
        FxTimingBar.Items.Clear();
        var names = st.Timings.Count > 0 ? st.Timings.Distinct().ToList() : new List<string> { "连锁发动" };
        foreach (var n in names)
            FxTimingBar.Items.Add(new SelectorBarItem { Text = n });
        if (FxTimingBar.Items.Count > 0) FxTimingBar.SelectedItem = FxTimingBar.Items[0];
    }

    private void UpdateOfficialButtons()
    {
        if (FxOfficialPreviewButton is null) return;
        var pick = _officialPick;
        bool can = _current is not null && pick is not null;
        // 内置的那 4 条解不出音频（fmod 不认它们的 FSB），列表里整行不可点
        FxOfficialPreviewButton.IsEnabled = can && pick is not null && !pick.Builtin;
        FxApplyOfficialButton.IsEnabled = can;
        FxOfficialPickButton.Content = pick?.Title ?? "选择音效…";
    }

    private void UpdatePresetButtons()
    {
        if (FxPreviewButton is null) return;
        var pick = _presetPick;
        bool can = _current is not null && pick is not null;
        // 素材可能是会话里导入过的临时路径，文件被移走了就别让试听按钮亮着
        FxPreviewButton.IsEnabled = can && File.Exists(pick!.Path);
        FxApplyPresetButton.IsEnabled = can;
        // 带来源前缀：导入的指向硬盘上那个文件，预设的是存过程序目录的副本
        FxPresetPickButton.Content = pick is null
            ? "选择预设或导入…"
            : pick.Local ? $"导入：{pick.Title}" : $"预设：{pick.Title}";
    }

    // ------------------------------------------------------------ 选择弹窗

    /// <summary>弹窗里的一行。官方槽、预设、导入各占一种来源</summary>
    private sealed class SoundPickRow
    {
        public string Title = "";
        public string Tag = "";
        public bool Builtin;
        /// <summary>本次会话导入的那个文件，不是游戏里的槽</summary>
        public bool Local;
        public string Label = "";
        public string Path = "";
        public double Seconds;
        /// <summary>「18 秒」。读不到时长就不显示，别占一个「0 秒」的位置</summary>
        public string Duration => Seconds > 0 ? $"{Seconds:0.#} 秒" : "";
    }

    private async void FxOfficialPick_Click(object sender, RoutedEventArgs e) => await PickOfficialAsync();

    private async void FxPresetPick_Click(object sender, RoutedEventArgs e)
    {
        // async void 里未捕获的异常会直接结束进程。2026-11-14 用户实测：
        // 命名弹窗里一个空引用就让整个软件闪退，连报错都来不及显示。
        try
        {
            await PickPresetAsync();
        }
        catch (Exception ex)
        {
            App.AddLog("ERROR", $"音效弹窗: {ex}");
            await InfoAsync("音效弹窗出错", ex.Message);
        }
    }

    private async Task PickOfficialAsync()
    {
        if (_current is null) return;
        if (!_slotsLoaded) await RefreshSoundAsync();
        var origins = _cacheOriginLabels();
        var rows = _slots
            .Where(s => !origins.Contains(s.Label))
            .Where(s => !s.Label.StartsWith(SoundOpsCustomPrefix, StringComparison.Ordinal)
                        || _current is null
                        || s.Label != $"SE_EV_CUSTOM{_current.Id}")
            .Select(s => new SoundPickRow
            {
                Title = s.CardText + (s.Cards.Count > 1 ? "" : ""),
                Tag = s.Builtin ? "内置 · 不可用" : $"{s.Cards.Count} 张卡在用",
                Builtin = s.Builtin,
                Label = s.Label,
            })
            .ToList();
        // 同名槽（_R 与 _P）在卡名一样时补后缀，否则两行看起来一模一样
        var dup = rows.GroupBy(r => r.Title).Where(g => g.Count() > 1).Select(g => g.Key).ToHashSet();
        foreach (var r in rows.Where(r => dup.Contains(r.Title)))
        {
            var tail = LabelTail(r.Label);
            if (tail.Length > 0) r.Title = $"{r.Title} · {tail}";
        }
        var picked = await ShowSoundPickerAsync("选择音效", rows, "输入卡名或编号");
        if (picked is null) return;
        _officialPick = picked;
        UpdateOfficialButtons();
        FxSoundHintText.Text = $"已选「{picked.Title}」。先点「试听」听一遍，满意再点「应用」，会复制一份成为这张卡自己的音效。";
    }

    /// <summary>
    /// 自改音效弹窗。保存与删除都会改动素材库，而同一个 XamlRoot 不能同时挂两个
    /// ContentDialog，所以那些操作先把本弹窗收起，处理完再重开并刷新列表。
    /// 循环到用户「选一段」或「取消」为止。
    /// </summary>
    private async Task PickPresetAsync()
    {
        if (_current is null) return;

        while (true)
        {
            if (!_presetsLoaded) await LoadPresetsAsync(true);
            var result = await ShowPresetPickerOnceAsync();

            if (result.Kind == SoundPickKind.Cancel) return;

            if (result.Kind == SoundPickKind.Import)
            {
                // 只选文件，不写入。选完直接回到外层，导入的那一条成为当前选中项
                if (await PickLocalAudioAsync() is not { } local) return;
                SelectPresetRow(local);
                return;
            }

            if (result.Kind == SoundPickKind.Save)
            {
                if (_localFile is null) continue;
                await NextFrameAsync();   // 等选择弹窗卸载完，再弹命名框
                await SaveImportedAsPresetAsync(_localFile);
                await LoadPresetsAsync(true);
                await NextFrameAsync();   // 命名框退场后，再重开选择弹窗
                continue;   // 重开弹窗，新预设已经出现在「我的预设」里
            }

            if (result.Kind == SoundPickKind.Delete)
            {
                await NextFrameAsync();   // 删除确认也是弹窗，同理
                await DeletePresetAsync(result.Row!);
                await LoadPresetsAsync(true);
                await NextFrameAsync();   // 确认框退场后，再重开选择弹窗
                continue;
            }

            if (result.Kind == SoundPickKind.Pick && result.Row is { } picked)
            {
                SelectPresetRow(picked);
                return;
            }
        }
    }

    /// <summary>
    /// 排到 UI 下一帧。ContentDialog.Hide() 之后弹窗还在卸载过程中，
    /// 同一 XamlRoot 立刻 Show 另一个会抛异常。等一帧让前一个彻底退场。
    /// </summary>
    private Task NextFrameAsync()
    {
        var tcs = new TaskCompletionSource();
        DispatcherQueue.TryEnqueue(() => tcs.SetResult());
        return tcs.Task;
    }

    private void SelectPresetRow(SoundPickRow row)
    {
        _presetPick = row;
        if (!string.IsNullOrEmpty(row.Path)) SetPreview(row.Path);
        UpdatePresetButtons();
        FxSoundHintText.Text = $"已选「{row.Title}」。先点「试听」听一遍，满意再点「应用」写进游戏文件。";
    }

    /// <summary>搜索 + 列表。弹窗只负责「挑哪一段」，试听与应用在外层那一行。列表用代码拼，不走 XAML 模板</summary>
    private async Task<SoundPickRow?> ShowSoundPickerAsync(string title, List<SoundPickRow> rows, string placeholder)
    {
        SoundPickRow? picked = null;
        var panel = new StackPanel { Spacing = 2 };
        var search = new TextBox { PlaceholderText = placeholder, Margin = new Thickness(0, 0, 0, 8) };
        var host = new StackPanel { Width = 460 };
        host.Children.Add(search);
        host.Children.Add(new ScrollViewer
        {
            Content = panel,
            Height = 340,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        });

        var dlg = new ContentDialog
        {
            Title = title,
            Content = host,
            CloseButtonText = "取消",
            XamlRoot = XamlRoot,
        };

        void Fill()
        {
            panel.Children.Clear();
            var q = search.Text.Trim();
            foreach (var row in rows.Where(r => q.Length == 0 || r.Title.Contains(q, StringComparison.OrdinalIgnoreCase)))
            {
                var grid = new Grid { ColumnSpacing = 10 };
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

                grid.Children.Add(new TextBlock
                {
                    Text = row.Title,
                    FontSize = 13,
                    VerticalAlignment = VerticalAlignment.Center,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                });

                var chip = new Border
                {
                    CornerRadius = new CornerRadius(9),
                    Padding = new Thickness(8, 2, 8, 2),
                    VerticalAlignment = VerticalAlignment.Center,
                    Background = row.Local
                        ? new SolidColorBrush(Windows.UI.Color.FromArgb(38, 24, 90, 189))
                        : ThemeBrush(row.Builtin
                            ? "SystemFillColorCautionBackgroundBrush"
                            : "SystemFillColorSuccessBackgroundBrush"),
                    Child = new TextBlock
                    {
                        Text = row.Tag,
                        FontSize = 11,
                        Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
                    },
                };
                Grid.SetColumn(chip, 1);
                grid.Children.Add(chip);

                var item = new Button
                {
                    Content = grid,
                    HorizontalAlignment = HorizontalAlignment.Stretch,
                    HorizontalContentAlignment = HorizontalAlignment.Stretch,
                    Padding = new Thickness(10, 7, 8, 7),
                    // 内置音效解不开，既不能试听也不能复制，整行不可点
                    IsEnabled = !row.Builtin,
                    // 本地导入的文件不是游戏里的槽，用淡蓝底跟预设区分开
                    Background = row.Local
                        ? new SolidColorBrush(Windows.UI.Color.FromArgb(26, 24, 90, 189))
                        : ThemeBrush("ControlFillColorDefaultBrush"),
                };
                item.Click += (_, _) =>
                {
                    picked = row;
                    dlg.Hide();
                };
                panel.Children.Add(item);
            }
            if (panel.Children.Count == 0)
                panel.Children.Add(new TextBlock
                {
                    Text = "没有匹配的音效",
                    Margin = new Thickness(10, 10, 0, 0),
                    Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
                });
        }

        search.TextChanged += (_, _) => Fill();
        Fill();
        await dlg.ShowAsync();
        return picked;
    }

    private enum SoundPickKind { Cancel, Pick, Import, Save, Delete }

    private sealed class SoundPickResult
    {
        public SoundPickKind Kind = SoundPickKind.Cancel;
        public SoundPickRow? Row;
    }

    /// <summary>
    /// 自改音效弹窗：当前导入 / 我的预设 / 内置预设 三组。
    /// 行由「选中按钮 + 34 宽的图标按钮」并排组成，不嵌套 Button，
    /// 否则内层按钮的点击会被外层吞掉。规格与动画页的方案保存、删除一致。
    /// </summary>
    private async Task<SoundPickResult> ShowPresetPickerOnceAsync()
    {
        var result = new SoundPickResult();
        var panel = new StackPanel { Spacing = 4 };
        var search = new TextBox { PlaceholderText = "搜索音效名", Margin = new Thickness(0, 0, 0, 10) };
        // 480 加弹窗内边距仍落在 WinUI 原生 548 上限内，不用额外放宽。
        // 滚动条是覆盖式的，加宽躲不开它，靠内容右侧留 20px 给它让位。
        var host = new StackPanel { MinWidth = 480 };
        host.Children.Add(search);
        host.Children.Add(new ScrollViewer
        {
            Content = panel,
            Height = 340,
            Padding = new Thickness(0, 0, 20, 0),
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        });

        var dlg = new ContentDialog
        {
            Title = "选择自改音效",
            Content = host,
            CloseButtonText = "取消",
            XamlRoot = XamlRoot,
        };

        void Done(SoundPickKind kind, SoundPickRow? row = null)
        {
            result.Kind = kind;
            result.Row = row;
            dlg.Hide();
        }

        var green = new SolidColorBrush(Windows.UI.Color.FromArgb(255, 0x0F, 0x7B, 0x0F));
        var red = new SolidColorBrush(Windows.UI.Color.FromArgb(255, 0xC4, 0x2B, 0x1C));
        var dim = ThemeBrush("TextFillColorTertiaryBrush");

        void AddGroup(string caption, string? rightText = null, Action? onRight = null)
        {
            // 右侧留白统一交给 ScrollViewer.Padding，这里不再单独留，
            // 否则分组横线的右端会比下面的行短一截
            var g = new Grid { ColumnSpacing = 8, Margin = new Thickness(0, 12, 0, 6) };
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.Children.Add(new TextBlock
            {
                Text = caption, FontSize = 12,
                Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
                VerticalAlignment = VerticalAlignment.Center,
            });
            var line = new Border
            {
                Height = 1,
                Background = ThemeBrush("ControlStrokeColorDefaultBrush"),
                VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetColumn(line, 1);
            g.Children.Add(line);
            if (rightText is not null)
            {
                if (onRight is not null)
                {
                    var link = new HyperlinkButton { Padding = new Thickness(0), FontSize = 11 };
                    link.Content = rightText;
                    link.Click += (_, _) => onRight();
                    Grid.SetColumn(link, 2);
                    g.Children.Add(link);
                }
                else
                {
                    var t = new TextBlock
                    {
                        Text = rightText, FontSize = 11,
                        Foreground = dim,
                        VerticalAlignment = VerticalAlignment.Center,
                    };
                    Grid.SetColumn(t, 2);
                    g.Children.Add(t);
                }
            }
            panel.Children.Add(g);
        }

        FrameworkElement MakeRow(SoundPickRow row, string? glyph = null, Brush? glyphBrush = null,
                                 string? tip = null, Action? onIcon = null)
        {
            var line = new Grid { ColumnSpacing = 8 };
            line.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            line.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            var inner = new Grid { ColumnSpacing = 8 };
            inner.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            inner.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            inner.Children.Add(new TextBlock
            {
                Text = row.Title, FontSize = 13,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
            });
            if (!string.IsNullOrEmpty(row.Duration))
            {
                var dur = new TextBlock
                {
                    Text = row.Duration, FontSize = 11,
                    Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                };
                Grid.SetColumn(dur, 1);
                inner.Children.Add(dur);
            }

            var item = new Button
            {
                Content = inner,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Padding = new Thickness(10, 7, 10, 7),
                Background = row.Local
                    ? new SolidColorBrush(Windows.UI.Color.FromArgb(26, 24, 90, 189))
                    : ThemeBrush("ControlFillColorDefaultBrush"),
            };
            item.Click += (_, _) => Done(SoundPickKind.Pick, row);
            line.Children.Add(item);

            if (onIcon is not null)
            {
                var btn = new Button { Width = 34, Height = 32, Padding = new Thickness(0) };
                btn.Content = new FontIcon { FontSize = 16, Glyph = glyph, Foreground = glyphBrush };
                ToolTipService.SetToolTip(btn, tip ?? "");
                btn.Click += (_, _) => onIcon();
                Grid.SetColumn(btn, 1);
                line.Children.Add(btn);
            }
            else if (glyph is not null)
            {
                // 内置行放一个不带按钮的小锁，让三组的左边界与右边界都对齐
                var box = new Border
                {
                    Width = 34, Height = 32,
                    Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
                    Child = new FontIcon
                    {
                        FontSize = 12, Glyph = glyph, Foreground = glyphBrush,
                        HorizontalAlignment = HorizontalAlignment.Center,
                        VerticalAlignment = VerticalAlignment.Center,
                    },
                };
                Grid.SetColumn(box, 1);
                line.Children.Add(box);
            }
            return line;
        }

        FrameworkElement ImportEntry()
        {
            // 浅灰底把「导入」这个动作跟上面那些可选条目分开：它是入口，不是一条素材
            var b = new Button
            {
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Left,
                Padding = new Thickness(10, 8, 10, 8),
                Background = ThemeBrush("SubtleFillColorSecondaryBrush"),
                BorderBrush = ThemeBrush("ControlStrokeColorDefaultBrush"),
                Content = new TextBlock
                {
                    Text = "导入本地音频…", FontSize = 13,
                    Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
                },
            };
            b.Click += (_, _) => Done(SoundPickKind.Import);
            return b;
        }

        void Fill()
        {
            panel.Children.Clear();
            var q = search.Text.Trim();
            bool Match(string s) => q.Length == 0 || s.Contains(q, StringComparison.OrdinalIgnoreCase);

            // 1. 当前导入。只有一个槽位，再导入就是替换；源文件被挪走就不列出来
            var local = _localFile;
            bool localVisible = local is not null && File.Exists(local.Path) && Match(local.Title);
            if (localVisible || q.Length == 0)
            {
                AddGroup("当前导入",
                         localVisible ? "换一个…" : null,
                         localVisible ? () => Done(SoundPickKind.Import) : null);
                if (localVisible)
                    panel.Children.Add(MakeRow(local!, "\uE74E", green, "存为预设",
                        () => Done(SoundPickKind.Save, local)));
                else
                    panel.Children.Add(ImportEntry());
            }

            // 2. 我的预设。可删；后端已按存入时间倒序排好
            var mine = _presets.Where(p => !p.Builtin && Match(p.Name)).ToList();
            if (mine.Count > 0)
            {
                AddGroup("我的预设", $"{mine.Count} 个");
                foreach (var p in mine)
                {
                    var row = new SoundPickRow
                    {
                        Title = p.Name, Path = p.Path, Seconds = p.Seconds,
                    };
                    panel.Children.Add(MakeRow(row, "\uE74D", red, "删除预设",
                        () => Done(SoundPickKind.Delete, row)));
                }
            }

            // 3. 内置预设。能选能应用，只是不给删
            var builtin = _presets.Where(p => p.Builtin && Match(p.Name)).ToList();
            if (builtin.Count > 0)
            {
                AddGroup("内置预设", "随程序自带，不可删");
                foreach (var p in builtin)
                {
                    var row = new SoundPickRow
                    {
                        Title = p.Name, Path = p.Path, Seconds = p.Seconds, Builtin = true,
                    };
                    panel.Children.Add(MakeRow(row, "\uE72E", dim));
                }
            }

            if (panel.Children.Count == 0)
                panel.Children.Add(new TextBlock
                {
                    Text = "没有匹配的音效", FontSize = 13,
                    Margin = new Thickness(4, 12, 0, 0),
                    Foreground = ThemeBrush("TextFillColorSecondaryBrush"),
                });
        }

        search.TextChanged += (_, _) => Fill();
        Fill();
        await dlg.ShowAsync();
        return result;
    }

    /// <summary>命名弹窗。撞内置名或已有名都标红并禁用保存，由用户自己换名</summary>
    private async Task<string?> AskPresetNameAsync(string initial)
    {
        var box = new TextBox { Text = initial, PlaceholderText = "预设名字" };
        var err = new TextBlock
        {
            FontSize = 12,
            Foreground = new SolidColorBrush(Windows.UI.Color.FromArgb(255, 0xC4, 0x2B, 0x1C)),
            TextWrapping = TextWrapping.Wrap,
            Visibility = Visibility.Collapsed,
        };
        var stack = new StackPanel { Spacing = 8, Width = 320 };
        stack.Children.Add(new TextBlock { Text = "名字", FontSize = 12 });
        stack.Children.Add(box);
        stack.Children.Add(err);

        var dlg = new ContentDialog
        {
            Title = "存为预设",
            Content = stack,
            PrimaryButtonText = "保存",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = XamlRoot,
        };

        void Check()
        {
            var n = box.Text.Trim();
            var hit = _presets.FirstOrDefault(p => string.Equals(p.Name, n, StringComparison.OrdinalIgnoreCase));
            bool taken = hit is not null;
            bool bad = n.Length == 0 || taken;
            err.Text = n.Length == 0
                ? "名字不能为空"
                : hit is null
                    ? ""
                    : hit.Builtin
                        ? "这个名字已被内置预设占用，换一个"
                        : "这个名字已被使用，换一个";
            err.Visibility = bad ? Visibility.Visible : Visibility.Collapsed;
            dlg.IsPrimaryButtonEnabled = !bad;
        }

        box.TextChanged += (_, _) => Check();
        Check();
        var r = await dlg.ShowAsync();
        return r == ContentDialogResult.Primary ? box.Text.Trim() : null;
    }

    /// <summary>只复制原件到 data/sound/presets，不转码。应用时和内置预设走同一条路</summary>
    private async Task SaveImportedAsPresetAsync(SoundPickRow row)
    {
        if (App.Engine is not { } engine) return;
        var name = await AskPresetNameAsync(row.Title);
        if (name is null) return;
        try
        {
            await engine.CallAsync("sound.presets.save", new Dictionary<string, object?>
            {
                ["name"] = name,
                ["audio_path"] = row.Path,
            });
            FxSoundHintText.Text = $"已存为预设「{name}」。源文件挪走或删掉都不影响它。";
        }
        catch (Exception ex)
        {
            await InfoAsync("保存预设失败", ex.Message);
        }
    }

    private async Task DeletePresetAsync(SoundPickRow row)
    {
        if (row.Builtin || App.Engine is not { } engine) return;
        if (!await ConfirmAsync("删除预设", $"删除预设「{row.Title}」？", "删除",
                note: "只删素材库里的这份音频。已经应用到卡上的音效是独立的一份，不受影响。")) return;
        try
        {
            await engine.CallAsync("sound.presets.delete", new Dictionary<string, object?>
            {
                ["name"] = row.Title,
            });
            // 外层正选中这一条的话一起清掉，别留一个指向已删文件的按钮
            if (_presetPick is { } p && p.Path == row.Path)
            {
                _presetPick = null;
                _previewPath = "";
                UpdatePresetButtons();
            }
            FxSoundHintText.Text = $"已删除预设「{row.Title}」。";
        }
        catch (Exception ex)
        {
            await InfoAsync("删除预设失败", ex.Message);
        }
    }

    private async Task PreviewRowAsync(SoundPickRow row)
    {
        if (!string.IsNullOrEmpty(row.Path))
        {
            SetPreview(row.Path);
            PlayPreview();
            return;
        }
        if (string.IsNullOrEmpty(row.Label) || App.Engine is not { } engine) return;
        var data = await engine.CallAsync("sound.preview", new Dictionary<string, object?>
        {
            ["label"] = row.Label,
        });
        var path = Str(data, "path");
        if (string.IsNullOrEmpty(path)) return;
        SetPreview(path);
        PlayPreview();
    }

    /// <summary>这张卡各条记录现在指向哪些槽，用来把它们从可借列表里排除</summary>
    private List<string> _cacheOriginLabels()
    {
        if (_current is not null && _soundCache.TryGetValue(_current.Id, out var st))
            return st.OriginAll.Where(x => !string.IsNullOrEmpty(x)).Distinct().ToList();
        return new List<string>();
    }

    private const string SoundOpsCustomPrefix = "SE_EV_CUSTOM";

    // ------------------------------------------------------------ 应用与试听

    private async void FxPreviewOfficial_Click(object sender, RoutedEventArgs e)
    {
        if (_officialPick is not { } row) return;
        if (row.Builtin) return;
        FxOfficialPreviewButton.IsEnabled = false;
        FxSoundHintText.Text = "正在解码这段音效，约 2 秒…";
        try
        {
            await PreviewRowAsync(row);
        }
        catch (Exception ex)
        {
            await InfoAsync("试听失败", ex.Message);
        }
        finally
        {
            UpdateOfficialButtons();
        }
    }

    private async void FxApplyOfficial_Click(object sender, RoutedEventArgs e)
    {
        if (_officialPick is not { } row || _current is null) return;
        if (App.Engine is not { } engine) return;
        FxApplyOfficialButton.IsEnabled = false;
        try
        {
            // 复制一份成本卡自己的槽，不指向别人的槽：指向就是「借用」，
            // 两张卡共用一个文件，改一个动两个，跟删掉借用特效是同一条理由。
            await RunSoundJobAsync(() => engine.CallAsync("sound.adopt", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["label"] = row.Label,
            }), "音效已采用");
            _officialPick = null;
            DropSoundCache();
            UpdateSoundUi();
        }
        catch (Exception ex)
        {
            FxApplyOfficialButton.IsEnabled = true;
            await InfoAsync("换音效失败", ex.Message);
        }
    }

    private void FxPreview_Click(object sender, RoutedEventArgs e) => PlayPreview();

    private async void PlayPreview()
    {
        if (string.IsNullOrEmpty(_previewPath) || !File.Exists(_previewPath)) return;
        StopPreview();
        try
        {
            // 走 StorageFile 而不是 file:// URI：非打包桌面应用里这条路更稳，
            // 拿不到文件时会抛异常，能直接告诉用户而不是静默无声
            var file = await Windows.Storage.StorageFile.GetFileFromPathAsync(_previewPath);
            var player = new MediaPlayer();
            player.Source = MediaSource.CreateFromStorageFile(file);
            _preview = player;
            player.Play();
        }
        catch (Exception ex)
        {
            await InfoAsync("试听失败", ex.Message);
        }
    }

    private void SetPreview(string path)
    {
        _previewPath = path;
        _previewCard = _current?.Id ?? 0;
        if (FxPreviewButton is not null) FxPreviewButton.IsEnabled = File.Exists(path);
    }

    private void StopPreview()
    {
        if (_preview is null) return;
        try
        {
            _preview.Pause();
        }
        catch (Exception)
        {
            // 播放器已经释放过, 忽略
        }
        _preview.Dispose();
        _preview = null;
    }

    private async void FxApplyPreset_Click(object sender, RoutedEventArgs e)
    {
        if (_presetPick is not { } pick || _current is null) return;
        if (App.Engine is not { } engine) return;
        FxApplyPresetButton.IsEnabled = false;
        FxSoundHintText.Text = "正在写入…第一次给这张卡配音要重打包游戏资源，约 10 秒。";
        try
        {
            await RunSoundJobAsync(() => engine.CallAsync("sound.custom", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["audio_path"] = pick.Path,
            }), "音效已修改");
            _presetPick = null;
            DropSoundCache();
            UpdateSoundUi();
        }
        catch (Exception ex)
        {
            FxApplyPresetButton.IsEnabled = true;
            await InfoAsync("写入预设音效失败", ex.Message);
        }
    }

    /// <summary>选一个本地音频当候选，不写游戏文件</summary>
    private async Task<SoundPickRow?> PickLocalAudioAsync()
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.MusicLibrary };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        foreach (var ext in new[] { ".mp3", ".wav", ".flac", ".m4a", ".ogg" })
            picker.FileTypeFilter.Add(ext);
        var file = await picker.PickSingleFileAsync();
        if (file is null) return null;

        var row = new SoundPickRow
        {
            Title = Path.GetFileNameWithoutExtension(file.Name),
            Tag = "本地文件",
            Path = file.Path,
            Local = true,
        };
        _localFile = row;
        return row;
    }

    private async void FxSoundRestore_Click(object sender, RoutedEventArgs e) => await RestoreSoundAsync();

    private async Task RestoreSoundAsync()
    {
        if (_current is null || App.Engine is not { } engine) return;
        if (!FxSoundRestoreButton.IsEnabled) return;
        try
        {
            await RunSoundJobAsync(() => engine.CallAsync("sound.restore_card", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
            }), "音效已复原");
            DropSoundCache();
            UpdateSoundUi();
        }
        catch (Exception ex)
        {
            await InfoAsync("复原音效失败", ex.Message);
        }
        finally
        {
            // 失败也可能是「条目已经换过，原音对不上」：引擎那边已经清掉了过期记录，
            // 这里必须重读一次，按钮才会按新状态变灰，不然会一直亮着点了没反应。
            DropSoundCache();
            UpdateSoundUi();
        }
    }

    private sealed class FxSoundState
    {
        public bool HasEntry;
        public string Current = "";
        public string Origin = "";
        public bool Custom = false;
        /// <summary>自改音效用的素材名，状态文字里显示「自改音效：菲比啾比」</summary>
        public string Source = "";
        public List<string> Timings = new();
        // 一张卡可能有多条记录, 复原要按整组比, 不能只看第一条
        public List<string> CurrentAll = new();
        public List<string> OriginAll = new();
        public bool CanRestore;
    }

    private async Task<FxSoundState?> ReadSoundStateAsync(int cardId)
    {
        if (App.Engine is not { } engine) return null;
        try
        {
            var data = await engine.CallAsync("runfx.timings", new Dictionary<string, object?> { ["card"] = cardId });
            var st = new FxSoundState();
            if (data.TryGetProperty("entries", out var es) && es.ValueKind == JsonValueKind.Array)
            {
                st.HasEntry = es.GetArrayLength() > 0;
                foreach (var e in es.EnumerateArray())
                {
                    var nm = Str(e, "name");
                    if (!string.IsNullOrEmpty(nm)) st.Timings.Add(nm);
                }
            }
            FirstOf(data, "current", ref st.Current);
            FirstOf(data, "origin", ref st.Origin);
            st.CurrentAll = StringsOf(data, "current");
            st.OriginAll = StringsOf(data, "origin");
            st.Custom = data.TryGetProperty("has_custom", out var hc) && hc.ValueKind == JsonValueKind.True;
            st.Source = Str(data, "source");
            // 有原始记录、条数对得上、且确实与现在不同, 才谈得上复原
            st.CanRestore = st.HasEntry && st.OriginAll.Count > 0
                            && st.OriginAll.Count == st.CurrentAll.Count
                            && !st.OriginAll.SequenceEqual(st.CurrentAll);
            return st;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static void FirstOf(JsonElement obj, string key, ref string into)
    {
        if (!obj.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return;
        foreach (var x in arr.EnumerateArray())
        {
            if (x.ValueKind == JsonValueKind.String) { into = x.GetString() ?? ""; return; }
        }
    }

    private static List<string> StringsOf(JsonElement obj, string key)
    {
        var list = new List<string>();
        if (!obj.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return list;
        foreach (var x in arr.EnumerateArray())
            if (x.ValueKind == JsonValueKind.String) list.Add(x.GetString() ?? "");
        return list;
    }

    private void FxTimingBar_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        // 只切显示, 不动数据。一个时机对应一条独立记录
    }

    private void SectionBar_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        _mode = (sender.SelectedItem as SelectorBarItem)?.Tag as string == "runfx" ? "runfx" : "cutin";
        if (_filterReady) ApplyMode();
    }

    // --------------------------------------------------------------- 选素材
    private static IntPtr OwnerHwnd()
        => WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindow!);

    private async Task<string?> PickFileAsync()
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.VideosLibrary };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        foreach (var ext in new[] { ".mp4", ".gif", ".webm", ".mov", ".mkv" })
            picker.FileTypeFilter.Add(ext);

        var file = await picker.PickSingleFileAsync();
        if (file is null) return null;
        // 过滤器能绕过（粘贴完整路径、改扩展名），这里按内容再确认一次
        if (!await ImportCheck.EnsureAsync(XamlRoot, file.Path, "video")) return null;
        return file.Path;
    }

    private async Task<string?> PickFolderAsync(string want)
    {
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.PicturesLibrary };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        picker.FileTypeFilter.Add("*");

        var folder = await picker.PickSingleFolderAsync();
        if (folder is null) return null;
        if (!await ImportCheck.EnsureAsync(XamlRoot, folder.Path, want)) return null;
        return folder.Path;
    }

    private async void BrowseFile_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFileAsync() is { } path) SourceBox.Text = path;
    }

    private async void BrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync("frames") is { } path) SourceBox.Text = path;
    }

    private async void FxBrowseFile_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFileAsync() is not { } path) return;
        FxSourceBox.Text = path;
        await TryUseVideoTrackAsync(path);
    }

    /// <summary>
    /// 选中的视频若有音轨，抽出来当成「当前导入」的候选。只成为素材，
    /// 不写游戏文件；帧文件夹和无声视频静默跳过。
    /// </summary>
    private async Task TryUseVideoTrackAsync(string path)
    {
        if (App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("sound.track", new Dictionary<string, object?> { ["path"] = path });
            var wav = Str(data, "path");
            if (string.IsNullOrEmpty(wav)) return;
            _localFile = new SoundPickRow
            {
                Title = Path.GetFileNameWithoutExtension(path) + " 的音轨",
                Path = wav,
                Local = true,
            };
            // 只是候选，不自动选中、不写游戏文件。状态条提一句即可，
            // 不碰音效卡片底部的说明文字（切卡时会被状态刷新覆盖）
            SetStatus($"视频音轨已放进「当前导入」，可在「自改音效」里试听、应用或存为预设。");
        }
        catch (Exception ex)
        {
            // 抽音轨失败不该挡住选素材，记一笔就行
            App.AddLog("WARN", $"提取视频音轨失败：{ex.Message}");
        }
    }

    private async void FxBrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync("frames") is { } path) FxSourceBox.Text = path;
    }

    // --------------------------------------------------------------- 生成
    /// <summary>
    /// 写游戏文件前先确认游戏没开。拆帧、超分要好几分钟，等写盘那一步再报
    /// 「请先关闭游戏」，这几分钟就白扔了（2026-11-23 查出来的）。
    /// 查不到就放行：真开着的时候写盘那层还会再拦一次。
    /// </summary>
    private async Task<bool> EnsureGameClosedAsync()
    {
        if (App.DryRun || App.Engine is not { } engine) return true;
        try
        {
            var r = await engine.CallAsync("app.ready",
                new Dictionary<string, object?> { ["check_game"] = true });
            if (r.ValueKind == JsonValueKind.Object
                && r.TryGetProperty("game_running", out var run)
                && run.ValueKind == JsonValueKind.True)
            {
                await InfoAsync("游戏还开着",
                    "生成要写入游戏文件，请先完全关闭游戏（包括后台进程）再试。", danger: true);
                return false;
            }
        }
        catch (Exception ex)
        {
            App.AddLog("WARN", $"检查游戏是否运行失败：{ex.Message}");
        }
        return true;
    }

    private async void BuildButton_Click(object sender, RoutedEventArgs e)
    {
        if (_checking) return;
        _checking = true;
        // 预检几百毫秒，只把按钮按下去，不加转圈：200ms 的 spinner 会闪一下，
        // 比不显示更难看。真正长的那段由 SetRunning 的遮罩接管。
        BuildButton.IsEnabled = false;
        try
        {
            await BuildCutinAsync();
        }
        finally
        {
            BuildButton.IsEnabled = true;
            _checking = false;
        }
    }

    private async Task BuildCutinAsync()
    {
        if (_current is not { } card || App.Engine is not { } engine) return;

        if (card.HasOurs)
        {
            await InfoAsync("已经挂着动画了", "这张卡上的召唤动画是本工具加的。要换的话，先点右上角的「复原」。");
            return;
        }
        if (!await EnsureGameClosedAsync()) return;

        if (string.IsNullOrWhiteSpace(SourceBox.Text))
        {
            await InfoAsync("还没有选素材", "先选一个视频、GIF，或者一帧文件夹。");
            return;
        }
        if (!await CheckFpsAsync((int)Num(FramesBox, 120), Num(DurBox, 1.6))) return;
        // 自己没有动画、也不是借用底图的卡，必须挑一张来源卡。后端已经不猜默认卡了，
        // 这里先挡下来，免得跑到一半报错。
        if (DonorOf(card) is null)
        {
            await InfoAsync("还没有选来源卡", "在右上方用「源卡预设」挑一张。");
            return;
        }

        // 先问清「这次跑不跑得了」：缺组件、素材帧数不够、按参数算出超过两页，
        // 三条一次问完，只报第一条。顺序是有意的——参数压根不成立的活不该让用户
        // 先点一次「继续」才知道。预检走主进程直答，几百毫秒回来，不用先弹框充数。
        var pre = await ComponentAssets.CutinPrecheckAsync(
            "cutin.video", SourceBox.Text, SelectedScale() > 0,
            (int)Num(FramesBox, 120), (int)Num(CellBox, 1664));
        if (pre is null)
        {
            await InfoAsync("组件状态没查成",
                "引擎这一下没回话，没有开始任何写入。等几秒再点一次就好。", danger: true);
            return;
        }
        if (pre.Missing is { Count: > 0 })
        {
            await ComponentAssets.PromptAsync(XamlRoot, pre.Missing);
            return;
        }
        if (!string.IsNullOrEmpty(pre.Block))
        {
            await InfoAsync("失败", $"游戏文件没有改动。\n{pre.Block}", danger: true);
            return;
        }

        // 所有档位都问一次：写进游戏是不可忽略的动作。
        // 只有「自带官方动画」才是真的顶掉游戏资源，用覆盖专用文案；
        // 官方★（借用同名卡）单独说明底图不受影响；其余档位用普通文案。
        // 按卡片状态判，不按筛选档位名写死。
        var borrowed = card.Cutin == "shared";
        var ok = card.Official || borrowed
            ? await ConfirmCutinOverwriteAsync(borrowed, "继续生成")
            : await ConfirmGenerateAsync("召唤动画", "生成召唤动画");
        if (!ok) return;
        // 三档统一：官方自带、官方★借用、无动画都用来源卡覆盖，
        // 三种都要 force 才能通过后端「已有游戏动画」的检查（上层已弹过覆盖确认）。
        var force = card.Official || borrowed;

        var args = new Dictionary<string, object?>
        {
            ["card"] = card.Id,
            ["source"] = SourceBox.Text,
            ["n"] = (int)Num(FramesBox, 120),
            ["duration"] = Num(DurBox, 4.8),
            ["size"] = (int)Math.Round(HeightToUnits(Num(SizeBox, 1.0))),
            ["mode"] = "auto",
            ["pop"] = PopBox.IsChecked == true,
            // 位移按 % 屏宽 / % 屏高传，换算留给引擎：骨架尺寸随来源卡的 Spine 物体缩放
            // 折算过（0.09~0.28），同一个单位数在不同卡上屏幕上能差两倍
            ["offx_pct"] = Num(OffXBox, 0),
            ["offy_pct"] = Num(OffYBox, 0),
            ["height"] = Num(SizeBox, 1.0),
            ["zoom"] = Num(ZoomBox, 1.1),
            ["rotate"] = Num(RotBox, 0),
            ["cell"] = (int)Num(CellBox, 1664),
            ["force"] = force,
            ["dry_run"] = App.DryRun,
        };
        var scale = SelectedScale();
        args["sharp"] = scale > 0;
        args["sharp_scale"] = scale;
        // 曲线: 默认用内置串; 自定义看弹窗里填的值
        string curve;
        if (CurvePresetBox.SelectedIndex == 0)
        {
            curve = CurveSilvicola;
        }
        else
        {
            if (_customCurve.Length == 0)
            {
                await InfoAsync("还没有自定义曲线", "「效果」选了「自定义」后，点一下下拉会弹出输入框，填完再生成；或者改回「默认」。");
                return;
            }
            curve = _customCurve;
        }
        if (curve.Length > 0) args["pop_curve"] = curve;
        // 来源卡：官方卡用它自己（改本身），借用底图的卡固定用底图，
        // 其余用框里那张（换卡时按类型自动填，用户可改）
        args["donor"] = DonorOf(card);
        // 「低清纹理兼容」不在这传：引擎按设置页里的开关自己读，见 settings.compat_sd

        SetRunning(true);
        SetStatus(App.DryRun ? "开始生成（预演）…" : "开始生成…");
        string? resultTitle = null, resultBody = null;
        bool resultDanger = false;
        try
        {
            await engine.CallAsync("cutin.video", args);
            // 刷新放在遮罩期内，且不在刷新前写「生成完成」：ReloadAsync 会先
            // 覆盖这段文字，ApplyFilterAsync 最后又收起状态条，结果就是
            // 底部早早显示结束、文字随即消失。全部做完再写。
            await ReloadAsync(card.Id);
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : "生成完成");
            resultTitle = App.DryRun ? "预演完成" : "完成";
            resultBody = App.DryRun ? "流程跑通了，没有写游戏文件。"
                                    : $"{card.Title} 的召唤动画已生成。";
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("生成失败：" + ex.Message);
            // 运行日志里也留一份, 弹窗关掉之后还能用「复制日志」查到原因
            App.AddLog("ERROR", $"{card.Title} 生成失败：{ex.Message}");
            resultTitle = "失败";
            resultBody = $"游戏文件没有改动。\n{Short(ex.Message)}";
            resultDanger = true;
        }
        finally
        {
            SetRunning(false);
        }
        // 遮罩撤掉之后再弹结果框，别隔着一层灰告诉用户「完成了」
        if (resultTitle is not null)
        {
            SetStatus(resultDanger ? "生成失败" : "生成完成");
            if (!resultDanger && !App.DryRun)
            {
                // 生成完直接问一句，省得用户再回列表里找那枚播放图标
                if (await ConfirmAsync("生成完成",
                        $"{card.Title} 的召唤动画已生成。要在软件里看一遍吗？",
                        "预览", card.Title, CardMetaOf(card),
                        "之后也能在「自改动画」那一行点播放图标。"))
                {
                    await ShowCutinPreviewAsync(card.Id);
                }
            }
            else
            {
                await InfoAsync(resultTitle, resultBody ?? "", resultDanger);
            }
        }
    }

    private async void CancelButton_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        await engine.CallAsync("cancel");
    }

    private static double Num(NumberBox box, double fallback)
        => double.IsNaN(box.Value) ? fallback : box.Value;

    /// <summary>每秒最多放 120 帧。游戏里每秒用得着的最多 60 帧，再密只是白占图集。</summary>
    private const int MaxFps = 120;

    /// <summary>
    /// 帧数 ÷ 时长 超过 120 就拦下来，并给出两条具体出路（降帧数或加时长）。
    /// 返回 false 表示不该继续生成。
    /// </summary>
    private async Task<bool> CheckFpsAsync(int frames, double seconds)
    {
        if (seconds <= 0 || frames <= 0) return true;
        double fps = frames / seconds;
        if (fps <= MaxFps) return true;
        int maxFrames = Math.Max(4, (int)Math.Floor(MaxFps * seconds));
        double minSeconds = Math.Ceiling(frames / (double)MaxFps * 10) / 10;
        await InfoAsync("帧率超过 120",
            $"每秒 {fps:0} 帧（{frames} 帧 ÷ {seconds:0.0} 秒），上限是每秒 {MaxFps} 帧。\n\n"
            + $"要么把这 {seconds:0.0} 秒里的帧数降到 {maxFrames}，"
            + $"要么把时长加到 {minSeconds:0.0} 秒（{frames} 帧正好放得下）。");
        return false;
    }

    /// <summary>超分倍数：0 = 关闭，2 / 4 = 先放大再缩回。</summary>
    private int SelectedScale()
        => (UpscaleBox.SelectedItem as ComboBoxItem)?.Tag is string t && int.TryParse(t, out var v)
            ? v : 0;

    // 套用方案时会程序化改选项，别把它当成用户在用超分
    private bool _upscaleGuard;

    /// <summary>选了超分但组件没装：提示去下载，然后把选项退回「关闭」。</summary>
    private async void UpscaleBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_upscaleGuard || SelectedScale() <= 0) return;
        if (await ComponentAssets.EnsureAsync(XamlRoot, "cutin.video", sharp: true)) return;

        _upscaleGuard = true;
        UpscaleBox.SelectedIndex = 0;      // 组件没装就别在界面上显示"已选超分"
        _upscaleGuard = false;
    }

    /// <summary>标题行那枚「复原」：按当前模式分发到动画或特效的移除。</summary>
    private void Restore_Click(object sender, RoutedEventArgs e)
    {
        if (InCutinMode) RemoveButton_Click(sender, e);
        else FxRemoveButton_Click(sender, e);
    }

    // --------------------------------------------------------------- 移除
    private async void RemoveButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card || App.Engine is not { } engine) return;

        var ok = await ConfirmAsync("复原召唤动画",
            "回到本工具改动之前的样子，登记一并撤掉。",
            "复原", card.Title, CardMetaOf(card), "这一步不能撤销。");
        if (!ok) return;

        SetBusy(true, App.DryRun ? "正在移除（预演）…" : "正在移除…");
        SetLocked(true);
        string? resultTitle = null, resultBody = null;
        bool resultDanger = false;
        try
        {
            await engine.CallAsync("cutin.remove", new { card = card.Id, dry_run = App.DryRun });
            // 不在 Reload 之前写「已移除」：ReloadAsync 会覆盖这段文字，
            // ApplyFilterAsync 最后又把状态条收起，结果就是底部早早显示结束。
            // 复原后标签退回「官方/官方★/无」，刷新要在遮罩期内做完。
            await ReloadAsync(card.Id);
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : "已移除");
            resultTitle = App.DryRun ? "预演完成" : "完成";
            resultBody = App.DryRun ? "流程跑通了，没有写游戏文件。"
                                    : $"{card.Title} 已回到改动前的样子。";
        }
        catch (EngineException ex)
        {
            SetStatus("移除失败：" + ex.Message);
            resultTitle = "失败";
            resultBody = $"游戏文件没有改动。\n{Short(ex.Message)}";
            resultDanger = true;
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
        // 遮罩撤掉之后再弹结果框
        if (resultTitle is not null)
        {
            SetStatus(resultDanger ? "移除失败" : "已移除");
            await InfoAsync(resultTitle, resultBody ?? "", resultDanger);
        }
    }

    private async void RefreshButton_Click(object sender, RoutedEventArgs e)
    {
        // 刷新 = 重新读卡数据, 清空来源卡, 并取消选中任何卡 (回到「从左边选一张卡」, 不自动选第一张)
        DonorBox.Text = "";
        DonorPresetBox.SelectedIndex = -1;
        _donorFilledFor = -1;
        _all.Clear();
        // 它本身就是一次完整重读，冷启动或索引过期时要等几秒。之前只有底部状态条，
        // 列表和按钮全开着，看起来像点了没反应。和其它长任务用同一套遮罩。
        SetLocked(true);
        try
        {
            await ReloadAsync();
        }
        finally
        {
            SetLocked(false);
        }
        // ReloadAsync 里的 KeepSelection 会兜底选第一张, 这里再清掉, 保持「未选择」
        CardList.SelectedIndex = -1;
        _current = null;
        ShowDetail(null);
    }

    // --------------------------------------------------------------- Spine 导入
    private async void SpineBrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync("spine") is { } path) SpineSourceBox.Text = path;
    }

    private async void SpineBrowseFile_Click(object sender, RoutedEventArgs e)
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.Downloads };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        foreach (var ext in new[] { ".zip", ".bundle", ".ab", ".unity3d", ".assetbundle" })
            picker.FileTypeFilter.Add(ext);

        var file = await picker.PickSingleFileAsync();
        if (file is null) return;
        if (!await ImportCheck.EnsureAsync(XamlRoot, file.Path, "spine")) return;
        SpineSourceBox.Text = file.Path;
    }

    /// <summary>选好来源才往下走，空的时候提示一次就停。</summary>
    private async Task<bool> RequireSpineSourceAsync()
    {
        if (!string.IsNullOrWhiteSpace(SpineSourceBox.Text)) return true;
        await InfoAsync("还没有选来源", "先选一个 Spine 导出文件夹、zip 或 bundle。");
        return false;
    }

    private async void SpineImportButton_Click(object sender, RoutedEventArgs e)
    {
        if (_spineChecking) return;
        _spineChecking = true;
        SpineImportButton.IsEnabled = false;
        try
        {
            await ImportSpineAsync();
        }
        finally
        {
            SpineImportButton.IsEnabled = !_donorLocked;
            _spineChecking = false;
        }
    }

    private async Task ImportSpineAsync()
    {
        if (_current is not { } card || !await RequireSpineSourceAsync()) return;

        if (card.HasOurs)
        {
            await InfoAsync("已经挂着动画了", "这张卡上的召唤动画是本工具加的。要换的话，先点右上角的「复原」。");
            return;
        }

        // Spine 导入也是拿来源卡那套骨架克隆一份再换内容，无动画的卡同样要先选来源卡
        if (DonorOf(card) is null)
        {
            await InfoAsync("还没有选来源卡", "Spine 导入会复制来源卡那套骨架，先在右上方选一张有动画的卡。");
            return;
        }
        if (!await EnsureGameClosedAsync()) return;

        // 3.x 的骨架要靠转换器升到 4.2，没装就先引导去装
        if (!await ComponentAssets.EnsureAsync(XamlRoot, "spine", SpineSourceBox.Text)) return;

        // 官方档原来会连弹两个框（先「Spine 导入」再「覆盖官方」），合并成一次：
        // 覆盖官方就用覆盖专用文案，其余档位用普通文案，都把「导错可复原」说清。
        var borrowed = card.Cutin == "shared";
        var ok = card.Official || borrowed
            ? await ConfirmCutinOverwriteAsync(borrowed, "导入到这张卡")
            : await ConfirmAsync("导入 Spine 动画？",
                "把 Spine 里的动画导进这张卡，写进游戏。",
                "导入到这张卡", card.Title, CardMetaOf(card),
                "导错了点「复原」可以还原。");
        if (!ok) return;
        var force = card.Official || borrowed;

        await RunJobAsync("spine.port_card",
            new Dictionary<string, object?>
            {
                ["card"] = card.Id,
                ["source"] = SpineSourceBox.Text,
                ["donor"] = DonorOf(card),
                ["force"] = force,
                ["dry_run"] = App.DryRun,
            },
            "spine", "正在导入…", "已导入");
    }

    // 「全部导入」「检查已安装」已移除：Spine 导入只针对选中的单卡
    // （有官方动画就改官方动画，没有就克隆来源卡的动画再改）。

    // --------------------------------------------------------------- 发动特效

    private async void FxBuildButton_Click(object sender, RoutedEventArgs e)
    {
        if (_fxChecking) return;
        _fxChecking = true;
        FxBuildButton.IsEnabled = false;
        try
        {
            await BuildFxAsync();
        }
        finally
        {
            FxBuildButton.IsEnabled = true;
            _fxChecking = false;
        }
    }

    private async Task BuildFxAsync()
    {
        if (_current is not { } card) return;

        if (string.IsNullOrWhiteSpace(FxSourceBox.Text))
        {
            await InfoAsync("还没有选素材", "先选一个视频、GIF，或者一帧文件夹。");
            return;
        }
        if (card.HasOursRunfx)
        {
            await InfoAsync("已经挂着特效了", "这张卡上的发动特效是本工具加的。要重做先点右上角的「复原」。");
            return;
        }
        if (!await CheckFpsAsync((int)Num(FxFramesBox, 60), Num(FxDurBox, 2.0))) return;
        if (!await EnsureGameClosedAsync()) return;

        // 特效底盘只有一页材质槽，页数上限按 1 算。动画那边的预检同时查组件、素材帧数、
        // 页数三件，这里复用它，只是把上限换掉（2026-11-23 补的）。
        var preFx = await ComponentAssets.CutinPrecheckAsync(
            "runfx.video", FxSourceBox.Text, SelectedFxScale() > 0,
            (int)Num(FxFramesBox, 60), FxCellHeight(), maxPages: 1);
        if (preFx is null)
        {
            await InfoAsync("组件状态没查成",
                "引擎这一下没回话，没有开始任何写入。等几秒再点一次就好。", danger: true);
            return;
        }
        if (preFx.Missing is { Count: > 0 })
        {
            await ComponentAssets.PromptAsync(XamlRoot, preFx.Missing);
            return;
        }
        if (!string.IsNullOrEmpty(preFx.Block))
        {
            await InfoAsync("失败", $"游戏文件没有改动。\n{preFx.Block}", danger: true);
            return;
        }

        // 组件已经在上面那次预检里查过、也弹过下载入口了，这里不再问第二遍。
        // 抠像不按界面开关走：引擎用 auto 判定（自带透明通道直接用、有幕布才抠）。
        int upscale = SelectedFxScale();

        var ok = card.HasGameRunfx
            ? await ConfirmFxReplaceAsync(card, "生成发动特效")
            : await ConfirmGenerateAsync("发动特效", "生成发动特效");
        if (!ok) return;

        var args = new Dictionary<string, object?>
        {
            ["card"] = card.Id,
            ["source"] = FxSourceBox.Text,
            ["n"] = (int)Num(FxFramesBox, 60),
            ["cell"] = FxCellHeight(),
            ["duration"] = Num(FxDurBox, 2.0),
            // 跟动画页同一条判定：自带透明就用、有幕布就抠、都没有就整帧
            ["mode"] = "auto",
            ["sharp"] = upscale > 0,
            ["sharp_scale"] = upscale,
            // 音效独立成一栏了，生成动画时不碰声音
            ["video_audio"] = false,
            // 界面填的是「占屏高」，引擎按帧的宽高比换算成 m_MaxParticleSize（占屏宽）
            ["size_h"] = Num(FxSizeBox, 0.5),
            ["offset_x"] = FxOffsetX(FxOffXBox),
            ["offset_y"] = FxOffsetY(FxOffYBox),
            // 预览缓存要记界面的百分比：世界单位离开补偿曲线就读不懂
            ["offx_pct"] = Num(FxOffXBox, 0),
            ["offy_pct"] = Num(FxOffYBox, 0),
            ["rotation"] = Num(FxRotBox, 0),
            ["dry_run"] = App.DryRun,
        };

        await RunFxJobAsync("runfx.video", args, "正在生成发动特效…", "已生成", notify: true,
                            previewCard: card.Id, previewKind: "runfx",
                            previewText: $"{card.Title} 的发动特效已生成。要在软件里看一遍吗？");
    }

    /// <summary>特效的超分倍数：0 = 关闭。找不到就给 0。</summary>
    private int SelectedFxScale()
        => (FxUpscaleBox.SelectedItem as ComboBoxItem)?.Tag is string t && int.TryParse(t, out var v)
            ? v : 0;

    /// <summary>选了超分但组件没装：提示去下载，然后把选项退回「关闭」。</summary>
    private async void FxUpscaleBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_fxUpscaleGuard || SelectedFxScale() <= 0) return;
        if (await ComponentAssets.EnsureAsync(XamlRoot, "runfx.video", FxSourceBox.Text, sharp: true))
            return;

        _fxUpscaleGuard = true;
        FxUpscaleBox.SelectedIndex = 0;
        _fxUpscaleGuard = false;
    }

    // 套用方案时会程序化改选项，别把它当成用户在用超分
    private bool _fxUpscaleGuard;

    /// <summary>
    /// 界面上的左右移 / 上下移按「占屏幕的比例」填，这里换成粒子本地单位。
    ///
    /// 一堆常数都是标定帧实测出来的（60 张相同帧导入，量框中心落在哪一像素，
    /// 不是肉眼估）。到目前攒下三个点，全部来自当前这条代码路径：
    ///   58.19 单位 → 上移 303.5px      （填 25，框中心 494 → 190.5）
    ///   -8.82 单位 → 下移 76.0px       （只补枢轴偏置，框中心 494 → 570）
    ///   0 单位     → 基准 494，画面中心比屏幕中心高 46px
    /// 前两点拟合双曲线 shift = a·u/(1 + b·u)，a = 7.94，b = 0.008975，
    /// 两点回代严丝合缝，渐近上限 a/b = 884px = 屏高的 82%。
    ///
    /// 纵向必须走曲线：这个特效沿场地平面往上，越远每单位在屏上越短。
    /// 横向是直线 19.75px/单位（平行成像面，不吃透视），所以两轴不能共用。
    /// 零点斜率 7.94px/单位，跟横向 19.75 差 2.5 倍，比例符合相机俯角。
    ///
    /// 换算只能和同一次实测绑死：旧版 FxOffsetUnits 的 A/B 常数换过几轮，
    /// 拿旧截图的位移反推新代码的单位会得出 4.6 这种偏小 40% 的值（踩过）。
    /// </summary>
    private const double PxPerUnitX = 19.75;   // 1920 宽下，横向，直线
    private const double OffsetCurveA = 7.9396;
    private const double OffsetCurveB = 0.008975;
    private const double FxRefHeight = 1080.0;
    private const double FxRefWidth = 1920.0;

    /// <summary>
    /// 0 偏移时画面中心比屏幕中心高多少像素（1920×1080 基准）。这是粒子枢轴带来的，
    /// 补回来填 0 才是正中。
    ///
    /// 2026-11-21 用 #22159 三档实测（画面大小 0.5 / 0.75 / 1.0，各录一段量网格线中心）：
    ///   0.5  → 偏移 -5.505 单位，正中
    ///   0.75 → 偏移 -8.059 单位，偏低 24px
    ///   1.0  → 偏移 -10.49 单位，偏低 46.6px
    /// 从 0.75 与 1.0 反推「要多补多少才正中」，两档给出的目标偏移都是 -5.5 单位，
    /// 局部斜率也都是 9.35px/单位。也就是说这个偏差是**固定**的，
    /// 旧版按「框宽比例」（0.0479 × 画面大小 × 1920）放大补偿是错的：
    /// 画面 1.0 时补 92px 想抵 92px 的偏差，而偏差本身只有 46px，补过头又被曲线吃掉一半。
    /// 46px = 0.0479 × 960，也就是当年在 960 宽的框上量到的那个数，与画面大小无关。
    /// </summary>
    private const double PivotBiasPx = 46.0;

    /// <summary>把「想要的屏幕位移（px，正数向上）」反解成粒子单位。</summary>
    private static double FxShiftToUnits(double px)
    {
        return px / (OffsetCurveA - OffsetCurveB * px);
    }

    /// <summary>左右移：按屏宽百分比换单位，正数向右。</summary>
    private static double FxOffsetX(NumberBox box)
    {
        // 实测正数往左，与「正数向右」的界面约定相反，这里取负修正
        return -Num(box, 0) / 100.0 * FxRefWidth / PxPerUnitX;
    }

    /// <summary>上下移：按屏高百分比换单位，正数向上。位移从「正中」起算。</summary>
    private static double FxOffsetY(NumberBox box)
    {
        double target = Num(box, 0) / 100.0 * FxRefHeight;
        // 枢轴偏差是固定值，不随画面大小变（见 PivotBiasPx 的实测记录）
        return FxShiftToUnits(target - PivotBiasPx);
    }


    private Task<bool> ConfirmFxReplaceAsync(CardItem card, string primary)
        // 按状态判，不按档位名写死。官方★（借用）补一句底图不受影响。
        => ConfirmOverwriteAsync("发动特效", card.Runfx is "shared" or "borrowed", primary);

    private async void FxRemoveButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card || App.Engine is not { } engine) return;
        if (!card.HasOursRunfx) return;

        var ok = await ConfirmAsync("复原发动特效",
            "回到本工具改动之前的样子，登记一并撤掉。",
            "复原", card.Title, CardMetaOf(card), "这一步不能撤销。");
        if (!ok) return;

        SetBusy(true, App.DryRun ? "正在移除（预演）…" : "正在移除…");
        SetLocked(true);
        string? resultTitle = null, resultBody = null;
        bool resultDanger = false;
        try
        {
            var removed = await engine.CallAsync("runfx.remove", new { card = card.Id, dry_run = App.DryRun });
            int kept = removed.ValueKind == JsonValueKind.Object
                       && removed.TryGetProperty("kept", out var kv) && kv.ValueKind == JsonValueKind.Number
                       ? kv.GetInt32() : 0;
            // 同动画复原：Reload 之前不写完成文字，刷新完再写，避免被状态条收起。
            // 复原后标签退回「官方/官方★/无」，刷新要在遮罩期内做完。
            await ReloadAsync(card.Id);
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : "已移除");
            resultTitle = App.DryRun ? "预演完成" : "完成";
            if (kept > 0)
            {
                // 游戏表里那条没清掉, 文件只好留着。不说一声的话, 用户只会看到卡片
                // 标签不对, 却不知道发生了什么。
                resultTitle = "已移除，但有残留";
                resultBody = $"{card.Title} 的特效条目没从游戏的表里清掉，" +
                             $"为了不让对局报错，留了 {kept} 个文件没有删。\n\n" +
                             "这几个文件不影响对局，游戏里这张卡会显示成官方特效。";
                resultDanger = true;
            }
            else
            {
                resultBody = App.DryRun ? "流程跑通了，没有写游戏文件。"
                                        : $"{card.Title} 已回到改动前的样子。";
            }
        }
        catch (EngineException ex)
        {
            SetStatus("移除失败：" + ex.Message);
            resultTitle = "失败";
            resultBody = $"游戏文件没有改动。\n{Short(ex.Message)}";
            resultDanger = true;
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
        // 遮罩撤掉之后再弹结果框
        if (resultTitle is not null)
        {
            SetStatus(resultDanger ? "移除失败" : "已移除");
            await InfoAsync(resultTitle, resultBody ?? "", resultDanger);
        }
    }

    private async void FxCancelButton_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        await engine.CallAsync("cancel");
    }

    /// <summary>跑一个动作，处理取消与失败，跑完刷新卡片状态。三个区共用。</summary>
    private async Task<JsonElement> RunJobAsync(string op, Dictionary<string, object?> args,
        string area, string running, string done, bool reload = true, bool notify = false,
        int previewCard = 0, string previewKind = "cutin", string? previewText = null)
    {
        if (App.Engine is not { } engine) return default;

        SetRunning(true, area);
        SetStatus(App.DryRun ? running.Replace("…", "（预演）…") : running);
        JsonElement data = default;
        // 结果框一律留到 finally 之后弹：遮罩亮着的时候弹「完成」，用户是隔着
        // 一层灰看到它，遮罩撤掉的瞬间又像跳了一下。先把整页恢复可操作再告知。
        string? resultTitle = null, resultBody = null;
        bool resultDanger = false;
        try
        {
            data = await engine.CallAsync(op, args);
            // 这里不写「已完成」。ReloadAsync 会先 SetBusy("正在读取卡片状态…")
            // 覆盖它，最后 ApplyFilterAsync 又把整条 StatusStrip 收起来，
            // 于是结果就是「底部早早显示结束，然后文字自己消失」。
            // 刷新期间状态条由 SetBusy 管，全部做完再写最终文字。
            if (reload) await ReloadAsync(_current?.Id ?? 0);
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : done);
            if (notify)
            {
                resultTitle = App.DryRun ? "预演完成" : "完成";
                resultBody = App.DryRun ? "流程跑通了，没有写游戏文件。" : done + "。";
            }
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("失败：" + ex.Message);
            App.AddLog("ERROR", $"{op} 失败：{ex.Message}");
            if (notify)
            {
                resultTitle = "失败";
                resultBody = $"游戏文件没有改动。\n{Short(ex.Message)}";
                resultDanger = true;
            }
        }
        finally
        {
            SetRunning(false, area);
        }
        // SetRunning(false) 不改文字，但 ApplyFilterAsync 收过 StatusStrip，
        // 这里补一次让最终状态留在底部，别让结果只靠弹窗说一遍。
        if (resultTitle is not null)
            SetStatus(resultDanger ? "失败，游戏文件没有改动"
                : App.DryRun ? "预演结束，没有写游戏文件" : done);
        if (resultTitle is not null)
        {
            // 生成完直接问一句，省得用户再回列表里找那枚播放图标（和动画页同一套）
            if (!resultDanger && !App.DryRun && previewCard > 0)
            {
                if (await ConfirmAsync(resultTitle,
                        previewText ?? $"{(resultBody ?? "")}。要在软件里看一遍吗？", "预览"))
                    await ShowCutinPreviewAsync(previewCard, previewKind);
            }
            else
            {
                await InfoAsync(resultTitle, resultBody ?? "", resultDanger);
            }
        }
        return data;
    }

    private Task<JsonElement> RunFxJobAsync(string op, Dictionary<string, object?> args, string running,
                                            string done, bool notify = false, int previewCard = 0,
                                            string previewKind = "cutin", string? previewText = null)
        => RunJobAsync(op, args, "runfx", running, done, notify: notify,
                       previewCard: previewCard, previewKind: previewKind, previewText: previewText);

    /// <summary>
    /// 音效写入也上同一张遮罩。给一张卡第一次配音要重打包 128MB 的 data.unity3d，
    /// 实测 7 到 10 秒；期间不挡页面的话，用户会连点或者切卡，写入的目标就对不上了。
    /// </summary>
    private async Task RunSoundJobAsync(Func<Task> body, string done)
    {
        SetRunning(true, "sound");
        try
        {
            await body();
            // 引擎进度会把最后一步「写文件」留在状态条上，遮罩撤掉后就成了残留的
            // 假进度。音效写入成功后在这里补一句最终状态，跟 RunJobAsync 对齐。
            SetStatus(done);
        }
        finally
        {
            SetRunning(false, "sound");
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

    // --------------------------------------------------------------- 对话框
    // 样式、按钮规则、预演说明都收敛在 Services.Dialogs，三页共用一份。
    private Task<bool> ConfirmAsync(string title, string body, string primary,
                                    string? cardName = null, string? cardMeta = null,
                                    string? note = null)
        => Dialogs.ConfirmAsync(XamlRoot, title, body, primary, cardName, cardMeta, note);

    private Task InfoAsync(string title, string body, bool danger = false)
        => Dialogs.InfoAsync(XamlRoot, title, body, danger);

    /// <summary>弹窗里的操作对象：卡名原样给，编号与卡种作为副信息。
    /// 名字别叫 CardName / CardMeta，XAML 里已有同名元素。</summary>
    private string? DlgCardName => _current?.Title;

    private string? DlgCardMeta => _current is null ? null : CardMetaOf(_current);

    private static string CardMetaOf(CardItem c)
        => string.IsNullOrEmpty(c.SubtypeCn) ? c.IdText : $"{c.IdText} · {c.SubtypeCn}";

    /// <summary>普通生成确认框：无／自改／自改★共用，写的是自己的东西。</summary>
    private Task<bool> ConfirmGenerateAsync(string what, string primary)
        => ConfirmAsync($"生成{what}？", "按当前设置写入游戏。", primary,
            DlgCardName, DlgCardMeta, "之后点「复原」可以还原。");

    /// <summary>覆盖官方资源的确认框：官方／官方★共用。这类是本工具唯一会顶掉
    /// 游戏自带资源的情形，必须单独说清楚；官方★（借用）再补一句底图不受影响。</summary>
    /// <summary>召唤动画现在一律克隆来源卡，文案要说清演出结构也会跟着换。</summary>
    private Task<bool> ConfirmCutinOverwriteAsync(bool borrowed, string primary)
        => ConfirmAsync("覆盖官方动画？",
            borrowed
                ? "现在播的是借用同名卡的官方动画。继续会单独建一套，那张卡不受影响。"
                : "自带官方动画。继续会整套换成来源卡的演出。",
            primary, DlgCardName, DlgCardMeta, "之后点「复原」可以还原。");

    private Task<bool> ConfirmOverwriteAsync(string what, bool borrowed, string primary)
        => ConfirmAsync($"覆盖官方{what}？",
            borrowed
                ? $"现在播的是借用同名卡的官方{what}。继续会给这一行单独建一套，" +
                  "官方那套不再播放，原来那张卡不受影响。"
                : $"自带官方{what}。继续会换成你的版本，官方那套不再播放。",
            primary, DlgCardName, DlgCardMeta, "之后点「复原」可以还原。");

    private async Task<string> PromptAsync(string title, string placeholder, string initial = "")
    {
        var box = new TextBox { PlaceholderText = placeholder, Text = initial };
        var dlg = new ContentDialog
        {
            Title = title,
            Content = box,
            PrimaryButtonText = "确定",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = XamlRoot,
        };
        var r = await dlg.ShowAsync();
        return r == ContentDialogResult.Primary ? box.Text : "";
    }

    // --------------------------------------------------------------- 状态
    private void ShowFatal(string message)
    {
        EmptyState.Visibility = Visibility.Visible;
        SetStatus(message);
    }

    /// <summary>跑任务时锁住表单，并把「取消」换出来。三个区各锁各的。</summary>
    /// <summary>遮罩上的取消：整页锁住之后，原来的取消按钮点不到了，这里留一个出口。</summary>
    private async void OverlayCancel_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is { } engine) await engine.CallAsync("cancel");
    }

    /// <summary>整页上锁：长任务期间不让改参数、不让切换召唤动画/发动特效，也不让碰 Spine 导入。
    /// 左侧导航在窗口壳里，仍然可以切到别的页。</summary>
    private void SetLocked(bool locked)
    {
        BusyOverlay.Visibility = locked ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SetRunning(bool running, string area = "cutin")
    {
        _jobActive = running;   // 本页所有任务的唯一进出点，进度广播按它放行
        SetLocked(running);
        switch (area)
        {
            case "sound":
                // 音效写入不改动生成表单，只需要挡住整页
                RestoreButton.IsEnabled = !running;
                break;
            case "runfx":
                FxBuildButton.IsEnabled = !running;
                FxBuildButton.Content = running ? "生成中…" : "生成发动特效";
                FxCancelButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
                FxSourceBox.IsEnabled = !running;
                FxFramesBox.IsEnabled = !running;
                FxQualityBox.IsEnabled = !running;
                FxDurBox.IsEnabled = !running;
                FxSizeBox.IsEnabled = !running;
                FxOffXBox.IsEnabled = !running;
                FxOffYBox.IsEnabled = !running;
                FxRotBox.IsEnabled = !running;
                FxUpscaleBox.IsEnabled = !running;
                RestoreButton.IsEnabled = !running && _current?.HasOursRunfx == true;
                break;
            case "spine":
                SpineImportButton.IsEnabled = !running && !_donorLocked;
                SpineImportButton.Content = running ? "导入中…"
                    : (_donorLocked ? "来源卡不可修改" : "导入到这张卡");
                SpineSourceBox.IsEnabled = !running;
                FxCancelButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
                RestoreButton.IsEnabled = !running && _current?.HasOurs == true;
                break;
            default:
                BuildButton.IsEnabled = !running && !_donorLocked;
                BuildButton.Content = running ? "生成中…"
                    : (_donorLocked ? "来源卡不可修改" : "生成召唤动画");
                CancelButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
                SetCutinFormEnabled(!running);
                RestoreButton.IsEnabled = !running && _current?.HasOurs == true;
                break;
        }
        _jobRunning = running;
        // 生成过程中不许切到「修改卡牌特效」页签，免得两个任务撞在一起
        if (FxTab is not null) FxTab.IsEnabled = !running;
        StatusStrip.Visibility = Visibility.Visible;
        // 任务结束要把进度条收掉。它平时靠引擎推 OnEngineProgress 亮起来，
        // 但那条路径只负责「显示」，没人负责「显示完了收起」——
        // 成功时会被随后 ReloadAsync 的整条状态条一起藏掉，失败时列表不刷新，
        // 那条蓝线就留在底部一直流动，看起来像还在跑。
        if (!running)
        {
            Progress.Visibility = Visibility.Collapsed;
            Progress.IsIndeterminate = false;
        }
    }

    /// <summary>生成期间锁住召唤动画这一整块表单（「取消」按钮不在此列）。</summary>
    private void SetCutinFormEnabled(bool on)
    {
        // 刷新 / 复原 不在这里：它们提到了标题行、两个模式共用，
        // 跟着动画表单一起禁用会连带把特效模式下的这两枚也关掉。
        // 任务进行中的启用状态由 SetRunning 单独管。
        foreach (var el in new Control?[]
                 {
                     SourceBox, BrowseFileButton, BrowseFolderButton,
                     SchemeBox, SchemeSaveButton, SchemeDeleteButton,
                     UpscaleBox, PopBox, CurvePresetBox,
                     DonorBox, DonorPresetBox,
                     FramesBox, DurBox, CellBox, SizeBox,
                     OffXBox, OffYBox, RotBox, ZoomBox,
                 })
        {
            if (el is not null) el.IsEnabled = on;
        }
    }

    private void SetBusy(bool busy, string? message)
    {
        StatusStrip.Visibility = busy || !string.IsNullOrEmpty(message)
            ? Visibility.Visible : Visibility.Collapsed;
        // 复位进度条: 不复位的话, 之后再有状态文本会把残留的转圈/半截条一起显示出来
        Progress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        Progress.IsIndeterminate = busy;
        Progress.Value = 0;
        Progress.Maximum = 100;
        if (message is not null) StatusText.Text = message;
    }

    // ------------------------------------------------------------- 来源卡
    private async Task LoadDonorPresetsAsync(EngineClient engine)
    {
        if (_donorPresets.Count > 0) return;
        try
        {
            var data = await engine.CallAsync("cutin.donors");
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("donors", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var d in arr.EnumerateArray())
                {
                    var id = d.TryGetProperty("mrk", out var m) ? m.GetInt32() : 0;
                    var label = d.TryGetProperty("label", out var l) ? l.GetString() ?? "" : "";
                    if (id > 0) _donorPresets.Add((label, id));
                }
            }
        }
        catch
        {
            // 读不到就不显示预设；来源卡框的自动填充不依赖它
        }
    }

    private void FillDonorCandidates()
    {
        _donorCandidates = _all.Where(c => !string.IsNullOrEmpty(c.Cutin)).ToList();
        DonorPresetBox.Items.Clear();
        foreach (var p in _donorPresets)
            DonorPresetBox.Items.Add(new ComboBoxItem { Content = p.Label, Tag = p.Id });
    }

    /// <summary>让「源卡预设」跟着自动填的来源卡走，两个控件不能各说各话。</summary>
    private void SyncDonorPreset(int id)
    {
        _donorSyncing = true;
        try
        {
            var idx = -1;
            for (var i = 0; i < DonorPresetBox.Items.Count; i++)
            {
                if (DonorPresetBox.Items[i] is ComboBoxItem { Tag: int t } && t == id)
                {
                    idx = i;
                    break;
                }
            }
            DonorPresetBox.SelectedIndex = idx;
        }
        finally
        {
            _donorSyncing = false;
        }
    }

    private void DonorPresetBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_donorSyncing) return;
        if (DonorPresetBox.SelectedItem is ComboBoxItem { Tag: int id })
        {
            var c = _donorCandidates.FirstOrDefault(x => x.Id == id);
            DonorBox.Text = c is not null ? $"{c.Id} {c.Name}" : id.ToString();
        }
    }

    /// <summary>
    /// 按卡片类型自动填来源卡（灵摆按第二个属性，规则在后端 cutin.donor 一处）。
    /// 换一张卡才重填一次：用户自己改过的来源卡，切回来要保留，不能被覆盖。
    /// </summary>
    private async Task FillDonorAsync(CardItem card)
    {
        if (App.Engine is not { } engine || _donorFilledFor == card.Id) return;
        _donorFilledFor = card.Id;
        try
        {
            var r = await engine.CallAsync("cutin.donor", new { card = card.Id });
            if (_current?.Id != card.Id) return;    // 等结果期间换卡了，作废
            var id = r.TryGetProperty("mrk", out var m) ? m.GetInt32() : 0;
            var name = r.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
            if (id > 0)
            {
                DonorBox.Text = name.Length > 0 ? $"{id} {name}" : id.ToString();
                SyncDonorPreset(id);
            }
        }
        catch
        {
            // 自动填不上就留给用户自己选，不弹窗打扰
        }
    }

    /// <summary>从来源卡框里取卡号。框里没填或不是数字就返回 null。</summary>
    private static int? ReadDonorId(TextBox box)
    {
        var t = (box.Text ?? "").Trim();
        var idPart = new string(t.TakeWhile(char.IsDigit).ToArray());
        return int.TryParse(idPart, out var id) && id > 0 ? id : null;
    }

    /// <summary>三档统一：官方动画、官方★、无动画都用来源卡框里那张（按卡种自动填）。</summary>
    private int? DonorOf(CardItem card) => ReadDonorId(DonorBox);

    // --------------------------------------------------------------- 方案
    private async Task LoadSchemesAsync(EngineClient engine)
    {
        try
        {
            var data = await engine.CallAsync("schemes.list");
            _schemes.Clear();
            if (data.TryGetProperty("schemes", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var s in arr.EnumerateArray())
                {
                    _schemes.Add(new SchemeItem
                    {
                        Name = s.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
                        Builtin = s.TryGetProperty("builtin", out var bi) && bi.ValueKind == JsonValueKind.True,
                        Frames = (int)Num2(s, "frames", 72),
                        Cell = (int)Num2(s, "cell", 1664),
                        Dur = Num2(s, "dur", 1.6),
                        Size = (int)Num2(s, "size", 2880),
                        OffX = (int)Num2(s, "offx", 0),
                        OffY = (int)Num2(s, "offy", 0),
                        Rot = (int)Num2(s, "rot", 0),
                        Zoom = Num2(s, "zoom", 1.0),
                        Upscale = (int)Num2(s, "upscale", 0),
                        Preset = (int)Num2(s, "preset", 0),
                        Curve = s.TryGetProperty("curve", out var c) ? c.GetString() ?? "" : "",
                        Pop = s.TryGetProperty("pop", out var p) && p.ValueKind == JsonValueKind.True,
                    });
                }
            }
            _schemeLoading = true;
            SchemeBox.Items.Clear();
            foreach (var s in _schemes)
                SchemeBox.Items.Add(new ComboBoxItem { Content = s.Name });
            if (SchemeBox.Items.Count > 0) SchemeBox.SelectedIndex = 0;
            _schemeLoading = false;
            // 选中第 0 项时事件被守卫拦掉了, 内置「默认」的参数要在这里手动填一次,
            // 否则界面显示「默认」但数字还是构造值 (120/1080/4.8/4800)。
            if (_schemes.Count > 0) ApplyScheme(0);
        }
        catch { /* 方案读不到不影响主流程 */ }
    }

    private void SchemeBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_schemeLoading) return;
        if (SchemeBox.SelectedIndex < 0 || SchemeBox.SelectedIndex >= _schemes.Count) return;
        ApplyScheme(SchemeBox.SelectedIndex);
    }

    /// <summary>下拉收起时再套用一次当前方案。用户改了参数后再点「默认」，选中项没变，
    /// SelectionChanged 不会触发，靠这个把它恢复回来。</summary>
    private void SchemeBox_DropDownClosed(object sender, object e)
    {
        if (_schemeLoading) return;
        if (SchemeBox.SelectedIndex < 0 || SchemeBox.SelectedIndex >= _schemes.Count) return;
        // 只是展开看一眼、没换项就不要重套：以前这里无条件套用，用户改完参数点开下拉
        // 看看有哪些方案，收起的一瞬间参数全被冲回原样（2026-11-23 查出来的）。
        if (SchemeBox.SelectedIndex == _appliedScheme) return;
        ApplyScheme(SchemeBox.SelectedIndex);
    }

    /// <summary>失败提示精简：只取第一行，超长截断。</summary>
    private static string Short(string msg)
    {
        var line = (msg ?? "").Split('\n')[0].Trim();
        return line.Length <= 160 ? line : line[..160] + "…";
    }

    private void ApplyScheme(int index)
    {
        if (index < 0 || index >= _schemes.Count) return;
        var s = _schemes[index];
        _appliedScheme = index;
        FramesBox.Value = s.Frames;
        CellBox.Value = s.Cell;
        DurBox.Value = s.Dur;
        SizeBox.Value = UnitsToHeight(s.Size);
        // 左右移/上下移现在是 % 屏宽/屏高（范围 ±75）。旧方案存的是角色高度倍数
        // （0.10 这种），读进来按百分比理解会偏小，界面上看一眼就知道要重填。
        // 见到超范围的当脏值处理，回零，免得挪出屏幕
        OffXBox.Value = Math.Abs(s.OffX) > 75 ? 0 : s.OffX;
        OffYBox.Value = Math.Abs(s.OffY) > 75 ? 0 : s.OffY;
        RotBox.Value = s.Rot;
        ZoomBox.Value = s.Zoom;
        PopBox.IsChecked = s.Pop;
        _upscaleGuard = true;
        UpscaleBox.SelectedIndex = s.Upscale switch { 2 => 1, 4 => 2, _ => 0 };
        _upscaleGuard = false;
        // 「自定义曲线」那一档会触发编辑弹窗（见 CurvePresetBox_SelectionChanged）。
        // 方案自带曲线，切方案时不该弹框，这里把事件拦掉。
        if (s.Preset >= 0)
        {
            _schemeLoading = true;
            CurvePresetBox.SelectedIndex = s.Preset;
            _schemeLoading = false;
        }
        if (s.Preset == 1) _customCurve = s.Curve;
        SyncCurveUi();
    }

    private async void SchemeSave_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        var name = await PromptAsync("保存方案", "新方案的名字：",
            SchemeBox.SelectedIndex >= 0 ? ((ComboBoxItem)SchemeBox.SelectedItem).Content?.ToString() ?? "" : "");
        if (string.IsNullOrWhiteSpace(name)) return;

        var item = new SchemeItem
        {
            Name = name.Trim(),
            Frames = (int)Num(FramesBox, 72),
            Cell = (int)Num(CellBox, 1664),
            Dur = Num(DurBox, 1.6),
            Size = (int)Math.Round(HeightToUnits(Num(SizeBox, 1.0))),
            OffX = (int)Num(OffXBox, 0),
            OffY = (int)Num(OffYBox, 0),
            Rot = (int)Num(RotBox, 0),
            Zoom = Num(ZoomBox, 1.1),
            Upscale = SelectedScale(),
            Preset = CurvePresetBox.SelectedIndex,
            Curve = _customCurve,
            Pop = PopBox.IsChecked == true,
        };
        // 内置方案（2K / 4K）不给覆盖，撞名就让用户换个名字
        if (_schemes.Any(s => s.Builtin && s.Name == item.Name))
        {
            SetStatus($"「{item.Name}」是内置方案，换个名字");
            return;
        }
        // 同名覆盖只在自己存的方案里找
        var idx = _schemes.FindIndex(s => !s.Builtin && s.Name == item.Name);
        if (idx >= 0) _schemes[idx] = item;
        else _schemes.Add(item);
        await SaveSchemesAsync(engine);
        SetStatus($"方案「{item.Name}」已保存");
    }

    private async void SchemeDelete_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        if (SchemeBox.SelectedIndex < 0 || SchemeBox.SelectedIndex >= _schemes.Count) return;
        var s = _schemes[SchemeBox.SelectedIndex];
        if (s.Builtin) return;   // 内置方案（2K / 4K）不给删
        if (!await ConfirmAsync("删除方案", $"删除方案「{s.Name}」？", "删除")) return;
        _schemes.RemoveAt(SchemeBox.SelectedIndex);
        await SaveSchemesAsync(engine);
        SetStatus($"方案「{s.Name}」已删除");
    }

    private async Task SaveSchemesAsync(EngineClient engine)
    {
        // 内置方案不写盘（2K / 4K 都在代码里）
        var custom = _schemes.Where(s => !s.Builtin).Select(s => (object)new Dictionary<string, object?>
        {
            ["name"] = s.Name, ["frames"] = s.Frames, ["cell"] = s.Cell,
            ["dur"] = s.Dur, ["size"] = s.Size, ["offx"] = s.OffX, ["offy"] = s.OffY,
            ["rot"] = s.Rot, ["zoom"] = s.Zoom, ["upscale"] = s.Upscale,
            ["preset"] = s.Preset, ["curve"] = s.Curve, ["pop"] = s.Pop,
        }).ToList();
        await engine.CallAsync("schemes.save", new Dictionary<string, object?> { ["schemes"] = custom });

        // 刷新下拉。_schemeLoading 会拦掉 SelectionChanged, 所以重建后必须手动 ApplyScheme,
        // 否则删掉一个方案后点「默认」数字不会刷新 (之前就是这个 bug)。
        _schemeLoading = true;
        var sel = Math.Min(Math.Max(SchemeBox.SelectedIndex, 0), _schemes.Count - 1);
        SchemeBox.Items.Clear();
        foreach (var s in _schemes)
            SchemeBox.Items.Add(new ComboBoxItem { Content = s.Name });
        SchemeBox.SelectedIndex = sel;
        _schemeLoading = false;
        ApplyScheme(sel);
    }

    private static double Num2(JsonElement e, string key, double fallback)
        => e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : fallback;

    // --------------------------------------------------------------- 曲线预设
    private void CurvePresetBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_schemeLoading) return;
        SyncCurveUi();
        if (CurvePresetBox.SelectedIndex == 1)
            _ = OpenCurveDialogAsync();
    }


    private void PopBox_Toggled(object sender, RoutedEventArgs e) => SyncCurveUi();

    /// <summary>勾了「弹出」才出现「效果」下拉；曲线输入框改成弹窗，不占参数区。</summary>
    private void SyncCurveUi()
    {
        if (PopBox is null || CurvePresetBox is null) return;
        var vis = PopBox.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        CurveLabel.Visibility = vis;
        CurvePresetBox.Visibility = vis;
        // 效果那一列始终保留等分的弹性宽度, 否则勾选/取消时另外两个下拉会跟着变宽变窄
    }

    /// <summary>选「自定义」时弹一个多行框填曲线，确定才存进 _customCurve；取消则退回默认。</summary>
    private async Task OpenCurveDialogAsync()
    {
        var box = new TextBox
        {
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinWidth = 440,
            MinHeight = 150,
            Text = _customCurve,
            PlaceholderText = "(0,25,60)(6,55,75)(15,92,92)",
        };
        var hint = new TextBlock
        {
            Text = "每段写 (帧,缩放,透明度)，百分比，段与段连着写；帧号只表示先后和比例，"
                 + "跟总帧数无关。「默认」那条是 25% 起、冲到 108%、回落到 76%、收尾 90%。",
            TextWrapping = TextWrapping.Wrap,
            FontSize = 12,
        };
        var content = new StackPanel { Spacing = 8 };
        content.Children.Add(box);
        content.Children.Add(hint);
        var dlg = new ContentDialog
        {
            Title = "自定义弹出曲线",
            Content = content,
            PrimaryButtonText = "确定",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = XamlRoot ?? (App.MainWindow?.Content as FrameworkElement)?.XamlRoot,
        };
        var r = await dlg.ShowAsync();
        if (r == ContentDialogResult.Primary)
            _customCurve = box.Text.Trim();
        else
            CurvePresetBox.SelectedIndex = 0;   // 取消就退回默认
    }

    /// <summary>列表里的播放图标：只对「自改动画」显示。</summary>
    private async void PlayPreview_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button b || b.Tag is not int card) return;
        await ShowCutinPreviewAsync(card);
    }

    /// <summary>
    /// 弹出动画预览。浮层挂在主窗口上，所以任何页面都能叫出来，
    /// 生成完在别的页面点「预览」也不用先切回本页。
    /// </summary>
    private async Task ShowCutinPreviewAsync(int card, string? kind = null)
    {
        if (App.MainWindow is not MainWindow shell) return;
        var item = _shown.FirstOrDefault(c => c.Id == card) ?? _all.FirstOrDefault(c => c.Id == card);
        if (await shell.ShowCutinPreviewAsync(card, item?.Title ?? $"#{card}",
                item is null ? "" : CardMetaOf(item), kind ?? _mode) is { } error)
            SetStatus(error);
    }

    private void SetStatus(string message)
    {
        StatusStrip.Visibility = Visibility.Visible;
        StatusText.Text = message;
    }
}
