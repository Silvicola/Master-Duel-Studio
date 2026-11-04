using System.Collections.ObjectModel;
using System.Text.Json;
using MDStudio.Models;
using MDStudio.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using Windows.Globalization.NumberFormatting;
using Windows.Storage.Pickers;

namespace MDStudio.Pages;

/// <summary>卡片主从页：左边列表，右边这张卡的详情与操作。</summary>
public sealed partial class AnimPage : Page
{
    /// <summary>源卡预设: 每类挑「骨骼最少的官方动画」(骨骼越少越不容易出错), 数据来自 2026-10-24 全量扫描。</summary>
    private static readonly (string Label, int Id)[] DonorPresets =
    {
        ("通常／效果", 3868),
        ("融合", 17443),
        ("仪式", 11377),
        ("同步", 7698),
        ("超量", 11016),
        ("灵摆", 18478),
        ("连接", 15036),
    };

    /// <summary>Silvicola 弹出曲线预设</summary>
    private const string CurveSilvicola =
        "(0,10,10)(7,40,45)(14,65,72)(21,85,90)(27,99,96)(32,110,100)(40,75,100)(46,85,100)(52,80,100)(68,82,100)";

    private List<CardItem> _donorCandidates = new();
    private List<SchemeItem> _schemes = new();
    private bool _schemeLoading;
    /// <summary>「效果 = 自定义」时用户填的曲线串，存在内存里，不写进每个 NumberBox。</summary>
    private string _customCurve = "";

    /// <summary>
    /// 视频自带的声音要落进一个游戏里已经存在的音效槽。全新的 label 在游戏里不响，
    /// 只能借一个现成的。这个值是后端 runfx_flip.DEFAULT_CARRIER。
    /// </summary>
    private const string FxCarrier = "SE_EV_EF09279_v1";

    private readonly ObservableCollection<CardItem> _shown = new();
    private readonly List<SoundSlot> _slots = new();
    private List<CardItem> _all = new();
    private CardItem? _current;
    /// <summary>有任务在跑。跑的时候整张表单锁死，也不许换卡。</summary>
    private bool _jobRunning;
    /// <summary>正在把列表选中项拨回原来那张，避免 SelectionChanged 递归。</summary>
    private bool _revertingSelection;
    private SoundSlot? _soundSlot;
    private bool _slotsLoaded;
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
        _filterReady = true;

        // XAML 里的 Value="4.8" 会先按 float 落成 4.800000019, 格式器又晚一步,
        // 所以这里重设格式器和值, 显示才干净。
        // 小数步进还必须带 IncrementNumberRounder: 只给 FractionDigits 时 NumberBox
        // 会先把值四舍五入成整数再加减, 上下键就走成 1 而不是 0.1。
        DurBox.NumberFormatter = DecimalFormatterFor(1, 0.1);
        DurBox.Value = 4.8;
        ZoomBox.NumberFormatter = DecimalFormatterFor(1, 0.1);
        ZoomBox.Value = 1.1;
        FxDurBox.NumberFormatter = DecimalFormatterFor(1, 0.1);
        FxDurBox.Value = 1.6;

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

        if (App.Engine is { } engine)
        {
            engine.LogReceived += OnEngineLog;
            engine.ProgressChanged += OnEngineProgress;
        }
        // 页面开了 NavigationCacheMode=Required, 切走再切回来是同一个实例,
        // 所以这里不随 Unloaded 退订: 否则离开期间进度和日志就断流了。

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
                     FxFramesBox, FxCellBox, FxDurBox,
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
    /// 找它，找到就装闩：TextBox 在焦点/文字变化时会把 Visibility 设回 Visible，一露头就按回去。
    /// </summary>
    private static void HideClearButton(NumberBox box)
    {
        if (TryLatchClearButton(box)) return;

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
        var del = FindByName(box, "DeleteButton");
        if (del is null) return false;
        del.Visibility = Visibility.Collapsed;
        del.RegisterPropertyChangedCallback(UIElement.VisibilityProperty, (_, _) =>
        {
            if (del.Visibility == Visibility.Visible) del.Visibility = Visibility.Collapsed;
        });
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
        if (_all.Count == 0) await ReloadAsync();
    }

    // ------------------------------------------------------------- 数据加载
    private async Task ReloadAsync()
    {
        if (App.Engine is not { } engine) return;
        SetBusy(true, "正在读取卡片状态…");
        try
        {
            var data = await engine.CallAsync("cards.status");
            _all = ParseCards(data);
            FillDonorCandidates();
            await LoadSchemesAsync(engine);
            await ApplyFilterAsync();
            KeepSelection();
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

    private static List<CardItem> ParseCards(JsonElement data)
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
            list.Add(new CardItem
            {
                Id = c.TryGetProperty("id", out var id) ? id.GetInt32() : 0,
                Name = Str(c, "name"),
                Cutin = Str(c, "cutin"),
                Runfx = Str(c, "runfx"),
            });
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

    /// <summary>状态栏里的筛选说明。没设条件时，列表页给一句范围说明，搜索结果页留空。</summary>
    private string FilterSuffix(bool idle)
    {
        var parts = new List<string>();
        foreach (var (box, all) in new[] { (CutinFilterBox, "全部动画"), (RunfxFilterBox, "全部特效") })
        {
            var text = (box.SelectedItem as ComboBoxItem)?.Content as string ?? all;
            if (text != all) parts.Add(text);
        }
        if (parts.Count > 0) return "（" + string.Join(" · ", parts) + "）";
        return idle ? "（有召唤动画或发动特效）" : "";
    }

    /// <summary>列表空了要给句话，否则一片空白看起来像卡住了。</summary>
    private void UpdateEmptyHint()
        => EmptyHint.Visibility = _shown.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

    private async Task ApplyFilterAsync()
    {
        var q = SearchBox.Text.Trim();
        var cutin = TagOf(CutinFilterBox);
        var runfx = TagOf(RunfxFilterBox);
        if (q.Length == 0)
        {
            // 没搜索词时本地就有全量, 直接筛, 不用绕一趟进程
            _shown.Clear();
            foreach (var c in _all)
            {
                if (cutin.Length > 0 && c.KindCutin != cutin) continue;
                if (runfx.Length > 0 && c.KindRunfx != runfx) continue;
                _shown.Add(c);
            }
            UpdateEmptyHint();
            SetBusy(false, $"共 {_shown.Count} 张{FilterSuffix(true)}");
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
        SetBusy(true, $"正在搜「{q}」…");
        try
        {
            var args = new Dictionary<string, object?> { ["q"] = q, ["limit"] = 200 };
            // 筛选交给后端: 只有后端知道匹配的全集, 前端筛会把 limit 之外的结果漏掉
            if (cutin.Length > 0) args["cutin"] = cutin;
            if (runfx.Length > 0) args["runfx"] = runfx;
            var data = await engine.CallAsync("cards.search", args);
            if (cts.IsCancellationRequested) return;

            var rows = ParseCards(data);
            _shown.Clear();
            foreach (var c in rows) _shown.Add(c);
            UpdateEmptyHint();
            SetBusy(false, $"「{q}」找到 {_shown.Count} 张{FilterSuffix(false)}");
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
        if (keep is not null && _shown.Contains(keep))
        {
            CardList.SelectedItem = keep;
        }
        else if (_shown.Count > 0)
        {
            CardList.SelectedIndex = 0;
        }
        else
        {
            _current = null;
            ShowDetail(null);
        }
    }

    // --------------------------------------------------------------- 选中
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
        _current = CardList.SelectedItem as CardItem;
        ShowDetail(_current);
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

        CardName.Text = card.Title;
        // 状态标签在左边列表里显示，这里只留卡号
        CardMeta.Text = card.IdText;

        // 已经有我们的动画才有东西可移除
        RemoveButton.IsEnabled = card.HasOurs;

        FxBar.Visibility = card.OfficialRunfx ? Visibility.Visible : Visibility.Collapsed;
        FxRemoveButton.IsEnabled = card.HasOursRunfx;
        FxRestoreButton.IsEnabled = card.HasOursRunfx;

        // 来源卡只在无官方动画/特效的卡上显示: 官方卡默认拿自己当来源卡, 不用选
        DonorPanel.Visibility = card.Official ? Visibility.Collapsed : Visibility.Visible;

        // 音效跟着当前卡走
        UpdateSoundUi();
        _ = RefreshSoundAsync();
    }

    // ------------------------------------------------------------------- 音效
    /// <summary>
    /// 读音效槽表。这张表跟卡片无关，整个会话只需读一次。
    /// 以前每次切换卡片都重跑一遍 IPC 并重建下拉框，点一下要等一截，就是这个原因。
    /// force=true 时强制重读（换完音效后槽的状态会变）。
    /// </summary>
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
            SoundSlotBox.ItemsSource = _slots;
            UpdateSoundUi();
        }
        catch (Exception)
        {
            // 音效读不出来不影响别的功能
        }
    }

    private void UpdateSoundUi()
    {
        var card = _current;
        if (card is null)
        {
            SoundCurrentText.Text = "未选择卡片";
            SoundInjectButton.IsEnabled = false;
            SoundUseButton.IsEnabled = false;
            SoundRestoreButton.IsEnabled = false;
            return;
        }
        if (!_slotsLoaded)
        {
            SoundCurrentText.Text = "正在读取音效…";
            SoundInjectButton.IsEnabled = false;
            SoundUseButton.IsEnabled = false;
            SoundRestoreButton.IsEnabled = false;
            return;
        }

        _soundSlot = _slots.FirstOrDefault(s => s.Cards.Contains(card.Id));
        if (_soundSlot is null)
        {
            SoundCurrentText.Text = "这张卡没有发动特效，没有音效可换。";
            SoundInjectButton.IsEnabled = false;
            SoundUseButton.IsEnabled = false;
            SoundRestoreButton.IsEnabled = false;
            return;
        }

        SoundCurrentText.Text = _soundSlot.OnDisk
            ? $"当前音效：{_soundSlot.CardText}（{_soundSlot.Cards.Count} 张卡在用）"
            : _soundSlot.Builtin
                ? $"当前音效：{_soundSlot.CardText}（内置音效，游戏里正常播放，本工具无法替换）"
                : $"当前音效：{_soundSlot.CardText}（音频文件没下载，换了也是静音）";
        // 下拉框默认选中这张卡正在用的槽, 免得还要自己找
        SoundSlotBox.SelectedItem = _soundSlot;
        SoundInjectButton.IsEnabled = _soundSlot.OnDisk;
        SoundUseButton.IsEnabled = _slots.Any(s => s.OnDisk);
        SoundRestoreButton.IsEnabled = true;
    }

    private async Task<string?> PickWavAsync()
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.MusicLibrary };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        picker.FileTypeFilter.Add(".wav");

        var file = await picker.PickSingleFileAsync();
        return file?.Path;
    }

    private async void SoundInject_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null || _soundSlot is null) return;
        if (!_soundSlot.OnDisk)
        {
            await InfoAsync("换不了这个音效", _soundSlot.Builtin
                ? $"{_soundSlot.CardText} 内置在游戏主程序里，没有独立文件可换。它在游戏里照常响。"
                : $"{_soundSlot.CardText} 的音频文件还没下载，换上去也是静音。");
            return;
        }

        var wav = await PickWavAsync();
        if (wav is null) return;

        var count = _soundSlot.Cards.Count;
        if (!await ConfirmAsync("换音效",
            $"把 {_soundSlot.CardText} 里的声音换成 {System.IO.Path.GetFileName(wav)}？\n" +
            $"用同一个音效槽的 {count} 张卡都会变。"))
            return;

        var args = new Dictionary<string, object?>
        {
            ["label"] = _soundSlot.Label,
            ["wav_path"] = wav,
            ["card"] = _current.Id,
        };
        await RunJobAsync("sound.inject", args, "runfx", "正在换音效…", "音效已替换");
        await RefreshSoundAsync(force: true);
    }

    private async void SoundUse_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null) return;
        if (SoundSlotBox.SelectedItem is not SoundSlot slot)
        {
            await InfoAsync("先选一个音效槽", "在下拉框里选一个音效槽，再点这个按钮。");
            return;
        }
        if (!slot.OnDisk)
        {
            await InfoAsync("换不了这个音效", slot.Builtin
                ? $"{slot.CardText} 内置在游戏主程序里，没有独立文件可换。它在游戏里照常响。"
                : $"{slot.CardText} 的音频文件还没下载，用了也是静音。");
            return;
        }

        var args = new Dictionary<string, object?>
        {
            ["card"] = _current.Id,
            ["label"] = slot.Label,
        };
        await RunJobAsync("runfx.set_sound", args, "runfx", "正在改音效槽…", "已改用 " + slot.CardText);
        await RefreshSoundAsync(force: true);
    }

    private async void SoundRestore_Click(object sender, RoutedEventArgs e)
    {
        if (_soundSlot is null) return;
        if (!await ConfirmAsync("还原音效", $"把 {_soundSlot.CardText} 恢复成游戏原本的声音？"))
            return;

        var args = new Dictionary<string, object?> { ["label"] = _soundSlot.Label };
        await RunJobAsync("sound.restore", args, "runfx", "正在还原…", "已还原");
        await RefreshSoundAsync(force: true);
    }

    private void SectionBar_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        var runfx = (sender.SelectedItem as SelectorBarItem)?.Tag as string == "runfx";
        CutinSection.Visibility = runfx ? Visibility.Collapsed : Visibility.Visible;
        RunfxSection.Visibility = runfx ? Visibility.Visible : Visibility.Collapsed;
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
        return file?.Path;
    }

    private async Task<string?> PickFolderAsync()
    {
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.PicturesLibrary };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        picker.FileTypeFilter.Add("*");

        var folder = await picker.PickSingleFolderAsync();
        return folder?.Path;
    }

    private async void BrowseFile_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFileAsync() is { } path) SourceBox.Text = path;
    }

    private async void BrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync() is { } path) SourceBox.Text = path;
    }

    private async void FxBrowseFile_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFileAsync() is { } path) FxSourceBox.Text = path;
    }

    private async void FxBrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync() is { } path) FxSourceBox.Text = path;
    }

    // --------------------------------------------------------------- 生成
    private async void BuildButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card || App.Engine is not { } engine) return;

        if (string.IsNullOrWhiteSpace(SourceBox.Text))
        {
            await InfoAsync("还没有选素材", "先选一个视频、GIF，或者一帧文件夹。");
            return;
        }

        // 官方卡会被覆盖，先问一句
        var force = false;
        if (card.Official)
        {
            var ok = await ConfirmAsync("覆盖官方召唤动画？",
                $"{card.Title} 自带官方召唤动画。继续会换成你的版本，官方那套不再播放。\n\n" +
                "之后点「移除」会还原。");
            if (!ok) return;
            force = true;
        }

        var args = new Dictionary<string, object?>
        {
            ["card"] = card.Id,
            ["source"] = SourceBox.Text,
            ["n"] = (int)Num(FramesBox, 120),
            ["duration"] = Num(DurBox, 4.8),
            ["size"] = (int)Num(SizeBox, 4800),
            ["mode"] = "auto",
            ["pop"] = PopBox.IsChecked == true,
            ["offset_x"] = Num(OffXBox, 0),
            ["offset_y"] = Num(OffYBox, 0),
            ["zoom"] = Num(ZoomBox, 1.1),
            ["rotate"] = Num(RotBox, 0),
            ["cell"] = (int)Num(CellBox, 1080),
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
        // 官方卡自带骨架, 用它自己当模板; 普通卡看来源卡框里填了什么
        if (card.Official)
            args["donor"] = card.Id;
        else if (ReadDonor(DonorBox, _donorCandidates) is { } donor)
            args["donor"] = donor.Id;

        SetRunning(true);
        SetStatus(App.DryRun ? "开始生成（预演）…" : "开始生成…");
        try
        {
            await engine.CallAsync("cutin.video", args);
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : "生成完成");
            await InfoAsync(App.DryRun ? "预演完成" : "生成完成",
                App.DryRun ? "流程跑通了，没有写游戏文件。" : $"{card.Title} 的召唤动画已生成。");
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("生成失败：" + ex.Message);
            // 历史页的日志里也留一份, 弹窗关掉之后还能查到原因
            App.AddLog("ERROR", $"{card.Title} 生成失败：{ex.Message}");
            await InfoAsync("生成失败", Short(ex.Message));
        }
        finally
        {
            SetRunning(false);
        }
        await ReloadAsync();
    }

    private async void CancelButton_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        await engine.CallAsync("cancel");
    }

    private static double Num(NumberBox box, double fallback)
        => double.IsNaN(box.Value) ? fallback : box.Value;

    /// <summary>超分倍数：0 = 关闭，2 / 4 = 先放大再缩回。</summary>
    private int SelectedScale()
        => (UpscaleBox.SelectedItem as ComboBoxItem)?.Tag is string t && int.TryParse(t, out var v)
            ? v : 0;

    // --------------------------------------------------------------- 移除
    private async void RemoveButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card || App.Engine is not { } engine) return;

        var ok = await ConfirmAsync("移除这张卡的召唤动画？",
            $"{card.Title}（{card.IdText}）会回到本工具改动之前的样子。");
        if (!ok) return;

        SetBusy(true, App.DryRun ? "正在移除（预演）…" : "正在移除…");
        SetLocked(true);
        try
        {
            await engine.CallAsync("cutin.remove", new { card = card.Id, dry_run = App.DryRun });
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : "已移除");
        }
        catch (EngineException ex)
        {
            SetStatus("移除失败：" + ex.Message);
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
        await ReloadAsync();
    }

    private async void RefreshButton_Click(object sender, RoutedEventArgs e)
    {
        // 刷新 = 重新读卡数据, 清空来源卡, 并取消选中任何卡 (回到「从左边选一张卡」, 不自动选第一张)
        DonorBox.Text = "";
        DonorPresetBox.SelectedIndex = -1;
        _all.Clear();
        await ReloadAsync();
        // ReloadAsync 里的 KeepSelection 会兜底选第一张, 这里再清掉, 保持「未选择」
        CardList.SelectedIndex = -1;
        _current = null;
        ShowDetail(null);
    }

    // --------------------------------------------------------------- Spine 导入
    private async void SpineBrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync() is { } path) SpineSourceBox.Text = path;
    }

    private async void SpineBrowseFile_Click(object sender, RoutedEventArgs e)
    {
        var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.Downloads };
        WinRT.Interop.InitializeWithWindow.Initialize(picker, OwnerHwnd());
        foreach (var ext in new[] { ".zip", ".bundle", ".ab", ".unity3d", ".assetbundle" })
            picker.FileTypeFilter.Add(ext);

        var file = await picker.PickSingleFileAsync();
        if (file is not null) SpineSourceBox.Text = file.Path;
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
        if (_current is not { } card || !await RequireSpineSourceAsync()) return;

        if (!await ConfirmAsync("Spine 导入",
            $"要把 Spine 里的动画导进 {card.Title} 吗？\n导错的话点「复原」可以还原。"))
            return;

        var force = false;
        if (card.Official)
        {
            var ok = await ConfirmAsync("覆盖官方召唤动画？",
                $"{card.Title} 自带官方召唤动画。继续会换成 Spine 里的版本，官方那套不再播放。\n\n" +
                "之后点「移除」会还原。");
            if (!ok) return;
            force = true;
        }

        await RunJobAsync("spine.port_card",
            new Dictionary<string, object?>
            {
                ["card"] = card.Id,
                ["source"] = SpineSourceBox.Text,
                ["force"] = force,
                ["dry_run"] = App.DryRun,
            },
            "spine", "正在导入…", "已导入");
    }

    // 「全部导入」「检查已安装」已移除：Spine 导入只针对选中的单卡
    // （有官方动画就改官方动画，没有就克隆来源卡的动画再改）。

    // --------------------------------------------------------------- 发动特效
    private async void FxBorrowButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card || App.Engine is not { } engine) return;

        if (card.HasOursRunfx)
        {
            await InfoAsync("已经挂着特效了", "这张卡上的发动特效是本工具加的。要换的话，先点下面的「复原」。");
            return;
        }

        JsonElement data;
        try
        {
            data = await engine.CallAsync("runfx.donors");
        }
        catch (Exception ex)
        {
            SetStatus("读取特效列表失败：" + ex.Message);
            return;
        }

        // 排除目标卡自己: 借自己的特效等于把官方那套再抄一遍, 没有意义
        var all = ParseEffects(data).Where(x => x.Id != card.Id).ToList();
        if (all.Count == 0)
        {
            await InfoAsync("没有可借的来源", "游戏里没找到带发动特效的卡。");
            return;
        }

        var shown = new ObservableCollection<EffectItem>();
        foreach (var x in all) shown.Add(x);

        var list = new ListView
        {
            ItemsSource = shown,
            ItemTemplate = (DataTemplate)Resources["EffectRow"],
            SelectionMode = ListViewSelectionMode.Single,
            MaxHeight = 420,
            MinWidth = 460,
        };
        var search = new TextBox { PlaceholderText = "搜卡名或卡号" };
        var body = new StackPanel { Spacing = 12 };
        body.Children.Add(search);
        body.Children.Add(list);

        var dlg = new ContentDialog
        {
            Title = $"选择特效来源（共 {all.Count} 张）",
            Content = body,
            PrimaryButtonText = "借用",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary,
            IsPrimaryButtonEnabled = false,
            XamlRoot = XamlRoot,
        };
        list.SelectionChanged += (_, _) => dlg.IsPrimaryButtonEnabled = list.SelectedItem is EffectItem;
        search.TextChanged += (_, _) =>
        {
            var q = search.Text.Trim();
            shown.Clear();
            foreach (var x in all)
            {
                if (q.Length == 0 ||
                    x.Id.ToString().Contains(q, StringComparison.Ordinal) ||
                    x.Name.Contains(q, StringComparison.OrdinalIgnoreCase))
                    shown.Add(x);
            }
            // 过滤到只剩一条时直接选中, 省一次点击
            if (shown.Count == 1) list.SelectedIndex = 0;
        };

        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;
        if (list.SelectedItem is not EffectItem src) return;

        if (!src.Ready)
        {
            await InfoAsync("这个特效的素材不在本地",
                $"{src.Title} 的特效文件还没下载，借过来游戏里也放不出来。先在对局里看它发动一次。");
            return;
        }

        var replace = false;
        if (card.OfficialRunfx && !await ConfirmFxReplaceAsync(card, src.Title)) return;
        replace = card.OfficialRunfx;

        await RunFxJobAsync("runfx.borrow",
            new Dictionary<string, object?>
            {
                ["card"] = card.Id,
                ["donor"] = src.Id,
                ["replace"] = replace,
                ["dry_run"] = App.DryRun,
            },
            "正在借用特效…", "已借用");
    }

    private async void FxBuildButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card) return;

        if (string.IsNullOrWhiteSpace(FxSourceBox.Text))
        {
            await InfoAsync("还没有选素材", "先选一个视频、GIF，或者一帧文件夹。");
            return;
        }
        if (card.HasOursRunfx)
        {
            await InfoAsync("已经挂着特效了", "这张卡上的发动特效是本工具加的。要重做先点「移除」。");
            return;
        }
        if (card.OfficialRunfx && !await ConfirmFxReplaceAsync(card, "你的视频")) return;

        var withSound = FxAudioBox.IsChecked == true;
        var args = new Dictionary<string, object?>
        {
            ["card"] = card.Id,
            ["source"] = FxSourceBox.Text,
            ["n"] = (int)Num(FxFramesBox, 60),
            ["cell"] = (int)Num(FxCellBox, 1080),
            ["duration"] = Num(FxDurBox, 1.6),
            // 发动特效是全屏播的，不抠图
            ["mode"] = "full",
            ["video_audio"] = withSound,
            ["dry_run"] = App.DryRun,
        };
        if (withSound) args["se_label"] = FxCarrier;

        await RunFxJobAsync("runfx.video", args, "正在生成发动特效…", "已生成", notify: true);
    }

    private Task<bool> ConfirmFxReplaceAsync(CardItem card, string what)
        => ConfirmAsync("覆盖官方发动特效？",
            $"{card.Title} 自带官方发动特效。继续会换成{what}，官方那套不再播放。\n\n" +
            "之后点「移除」会还原。");

    private async void FxRemoveButton_Click(object sender, RoutedEventArgs e)
    {
        if (_current is not { } card || App.Engine is not { } engine) return;
        if (!card.HasOursRunfx) return;

        var ok = await ConfirmAsync("移除这张卡的发动特效？",
            $"{card.Title}（{card.IdText}）会回到本工具改动之前的样子。");
        if (!ok) return;

        SetBusy(true, App.DryRun ? "正在移除（预演）…" : "正在移除…");
        SetLocked(true);
        try
        {
            await engine.CallAsync("runfx.remove", new { card = card.Id, dry_run = App.DryRun });
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : "已移除");
        }
        catch (EngineException ex)
        {
            SetStatus("移除失败：" + ex.Message);
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
        await ReloadAsync();
    }

    private async void FxCancelButton_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        await engine.CallAsync("cancel");
    }

    /// <summary>跑一个动作，处理取消与失败，跑完刷新卡片状态。三个区共用。</summary>
    private async Task<JsonElement> RunJobAsync(string op, Dictionary<string, object?> args,
        string area, string running, string done, bool reload = true, bool notify = false)
    {
        if (App.Engine is not { } engine) return default;

        SetRunning(true, area);
        SetStatus(App.DryRun ? running.Replace("…", "（预演）…") : running);
        JsonElement data = default;
        try
        {
            data = await engine.CallAsync(op, args);
            SetStatus(App.DryRun ? "预演结束，没有写游戏文件" : done);
            if (notify)
                await InfoAsync(App.DryRun ? "预演完成" : "完成",
                    App.DryRun ? "流程跑通了，没有写游戏文件。" : done + "。");
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("失败：" + ex.Message);
            App.AddLog("ERROR", $"{op} 失败：{ex.Message}");
            if (notify) await InfoAsync("失败", Short(ex.Message));
        }
        finally
        {
            SetRunning(false, area);
        }
        if (reload) await ReloadAsync();
        return data;
    }

    private Task<JsonElement> RunFxJobAsync(string op, Dictionary<string, object?> args, string running, string done, bool notify = false)
        => RunJobAsync(op, args, "runfx", running, done, notify: notify);

    private static List<EffectItem> ParseEffects(JsonElement data)
    {
        var list = new List<EffectItem>();
        if (data.ValueKind != JsonValueKind.Object ||
            !data.TryGetProperty("effects", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return list;

        foreach (var e in arr.EnumerateArray())
        {
            list.Add(new EffectItem
            {
                Id = e.TryGetProperty("id", out var id) ? id.GetInt32() : 0,
                Name = Str(e, "name"),
                Timing = Str(e, "timing"),
                Se = Str(e, "se"),
                Ready = !e.TryGetProperty("ready", out var r) || r.ValueKind != JsonValueKind.False,
            });
        }
        return list;
    }

    // --------------------------------------------------------------- 引擎事件
    private void OnEngineLog(string level, string text)
        => DispatcherQueue.TryEnqueue(() => App.AddLog(level, text));

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
        // 切到别的页面时本页的 XamlRoot 会变 null。任务在别的页面跑完也要能提示，
        // 所以退一步用窗口根元素的 XamlRoot。
        var root = XamlRoot ?? (App.MainWindow?.Content as FrameworkElement)?.XamlRoot;
        if (root is null)
        {
            App.AddLog("ERROR", $"{title}：{body}");
            return;
        }
        try
        {
            var dlg = new ContentDialog
            {
                Title = title,
                Content = body,
                CloseButtonText = "知道了",
                DefaultButton = ContentDialogButton.Close,
                XamlRoot = root,
            };
            await dlg.ShowAsync();
        }
        catch
        {
            // 已经有一个对话框开着时 ShowAsync 会抛异常。至少把内容落到日志里，别让它静默消失。
            App.AddLog("ERROR", $"{title}：{body}");
        }
    }

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
        SetLocked(running);
        switch (area)
        {
            case "runfx":
                FxBuildButton.IsEnabled = !running;
                FxBuildButton.Content = running ? "生成中…" : "生成发动特效";
                FxCancelButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
                FxSourceBox.IsEnabled = !running;
                FxFramesBox.IsEnabled = !running;
                FxCellBox.IsEnabled = !running;
                FxDurBox.IsEnabled = !running;
                FxAudioBox.IsEnabled = !running;
                FxRestoreButton.IsEnabled = !running && _current?.HasOursRunfx == true;
                break;
            case "spine":
                SpineImportButton.IsEnabled = !running;
                SpineImportButton.Content = running ? "导入中…" : "导入到这张卡";
                SpineSourceBox.IsEnabled = !running;
                FxCancelButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
                break;
            default:
                BuildButton.IsEnabled = !running;
                BuildButton.Content = running ? "生成中…" : "生成召唤动画";
                CancelButton.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
                SetCutinFormEnabled(!running);
                break;
        }
        _jobRunning = running;
        // 生成过程中不许切到「修改发动特效」页签，免得两个任务撞在一起
        if (FxTab is not null) FxTab.IsEnabled = !running;
        StatusStrip.Visibility = Visibility.Visible;
    }

    /// <summary>生成期间锁住召唤动画这一整块表单（「取消」按钮不在此列）。</summary>
    private void SetCutinFormEnabled(bool on)
    {
        foreach (var el in new Control?[]
                 {
                     SourceBox, BrowseFileButton, BrowseFolderButton,
                     SchemeBox, SchemeSaveButton, SchemeDeleteButton,
                     UpscaleBox, PopBox, CurvePresetBox,
                     DonorBox, DonorPresetBox, RefreshButton, RemoveButton,
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
    private void FillDonorCandidates()
    {
        _donorCandidates = _all.Where(c => !string.IsNullOrEmpty(c.Cutin)).ToList();
        DonorPresetBox.Items.Clear();
        foreach (var p in DonorPresets)
            DonorPresetBox.Items.Add(new ComboBoxItem { Content = p.Label, Tag = p.Id });
    }

    private static IEnumerable<CardItem> FilterCards(List<CardItem> src, string q)
    {
        q = (q ?? "").Trim();
        if (q.Length == 0) return src;
        return src.Where(c => c.Name.Contains(q, StringComparison.OrdinalIgnoreCase) ||
                              c.Id.ToString().Contains(q, StringComparison.Ordinal));
    }

    private void DonorBox_TextChanged(AutoSuggestBox sender, AutoSuggestBoxTextChangedEventArgs args)
    {
        if (args.Reason != AutoSuggestionBoxTextChangeReason.UserInput) return;
        sender.ItemsSource = FilterCards(_donorCandidates, sender.Text);
    }

    private static void DonorChosen(AutoSuggestBox sender, AutoSuggestBoxSuggestionChosenEventArgs args)
    {
        if (args.SelectedItem is CardItem c) sender.Text = $"{c.Id} {c.Name}";
    }

    private void DonorBox_SuggestionChosen(AutoSuggestBox sender, AutoSuggestBoxSuggestionChosenEventArgs args)
        => DonorChosen(sender, args);

    private void DonorPresetBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (DonorPresetBox.SelectedItem is ComboBoxItem { Tag: int id })
        {
            var c = _donorCandidates.FirstOrDefault(x => x.Id == id);
            DonorBox.Text = c is not null ? $"{c.Id} {c.Name}" : id.ToString();
        }
    }

    /// <summary>从来源卡框里取那张卡。框里没填或找不到就返回 null。</summary>
    private CardItem? ReadDonor(AutoSuggestBox box, List<CardItem> pool)
    {
        var t = (box.Text ?? "").Trim();
        if (t.Length == 0) return null;
        var idPart = new string(t.TakeWhile(char.IsDigit).ToArray());
        return int.TryParse(idPart, out var id) ? pool.FirstOrDefault(c => c.Id == id) : null;
    }

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
                        Frames = (int)Num2(s, "frames", 72),
                        Cell = (int)Num2(s, "cell", 1820),
                        Dur = Num2(s, "dur", 1.6),
                        Size = (int)Num2(s, "size", 2560),
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
            if (_schemes.Count > 0) ApplyScheme(_schemes[0]);
        }
        catch { /* 方案读不到不影响主流程 */ }
    }

    private void SchemeBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_schemeLoading) return;
        if (SchemeBox.SelectedIndex < 0 || SchemeBox.SelectedIndex >= _schemes.Count) return;
        ApplyScheme(_schemes[SchemeBox.SelectedIndex]);
    }

    /// <summary>下拉收起时再套用一次当前方案。用户改了参数后再点「默认」，选中项没变，
    /// SelectionChanged 不会触发，靠这个把它恢复回来。</summary>
    private void SchemeBox_DropDownClosed(object sender, object e)
    {
        if (_schemeLoading) return;
        if (SchemeBox.SelectedIndex < 0 || SchemeBox.SelectedIndex >= _schemes.Count) return;
        ApplyScheme(_schemes[SchemeBox.SelectedIndex]);
    }

    /// <summary>失败提示精简：只取第一行，超长截断。</summary>
    private static string Short(string msg)
    {
        var line = (msg ?? "").Split('\n')[0].Trim();
        return line.Length <= 160 ? line : line[..160] + "…";
    }

    private void ApplyScheme(SchemeItem s)
    {
        FramesBox.Value = s.Frames;
        CellBox.Value = s.Cell;
        DurBox.Value = s.Dur;
        SizeBox.Value = s.Size;
        OffXBox.Value = s.OffX;
        OffYBox.Value = s.OffY;
        RotBox.Value = s.Rot;
        ZoomBox.Value = s.Zoom;
        PopBox.IsChecked = s.Pop;
        UpscaleBox.SelectedIndex = s.Upscale switch { 2 => 1, 4 => 2, _ => 0 };
        if (s.Preset >= 0) CurvePresetBox.SelectedIndex = s.Preset;
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
            Cell = (int)Num(CellBox, 1820),
            Dur = Num(DurBox, 1.6),
            Size = (int)Num(SizeBox, 2560),
            OffX = (int)Num(OffXBox, 0),
            OffY = (int)Num(OffYBox, 0),
            Rot = (int)Num(RotBox, 0),
            Zoom = Num(ZoomBox, 1.1),
            Upscale = SelectedScale(),
            Preset = CurvePresetBox.SelectedIndex,
            Curve = _customCurve,
            Pop = PopBox.IsChecked == true,
        };
        // 同名覆盖
        var idx = _schemes.FindIndex(s => s.Name == item.Name);
        if (idx > 0) _schemes[idx] = item;        // 0 是内置, 不覆盖
        else _schemes.Add(item);
        await SaveSchemesAsync(engine);
        SetStatus($"方案「{item.Name}」已保存");
    }

    private async void SchemeDelete_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        if (SchemeBox.SelectedIndex <= 0) return;   // 内置方案不给删
        var s = _schemes[SchemeBox.SelectedIndex];
        if (!await ConfirmAsync("删除方案", $"删除方案「{s.Name}」？")) return;
        _schemes.RemoveAt(SchemeBox.SelectedIndex);
        await SaveSchemesAsync(engine);
        SetStatus($"方案「{s.Name}」已删除");
    }

    private async Task SaveSchemesAsync(EngineClient engine)
    {
        // 内置那条不写盘 (index 0)
        var custom = _schemes.Skip(1).Select(s => (object)new Dictionary<string, object?>
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
        ApplyScheme(_schemes[sel]);
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

    /// <summary>勾了「弹出效果」才出现「效果」下拉；曲线输入框改成弹窗，不占参数区。</summary>
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
            PlaceholderText = "(0,10,10)(7,40,45)(14,65,72)",
        };
        var hint = new TextBlock
        {
            Text = "每段写 (帧,缩放,透明度)，百分比，段与段连着写；帧号从左到右递增。",
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

    private void SetStatus(string message)
    {
        StatusStrip.Visibility = Visibility.Visible;
        StatusText.Text = message;
    }
}
