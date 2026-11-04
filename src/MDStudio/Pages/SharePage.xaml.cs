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

/// <summary>整合包页：导出你做过的卡、安装别人做的包、查看包内容。</summary>
public sealed partial class SharePage : Page
{
    private readonly ObservableCollection<PackCardItem> _exportable = new();
    private readonly ObservableCollection<InspectItem> _inspected = new();

    public SharePage()
    {
        InitializeComponent();
        ExportList.ItemsSource = _exportable;

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
        await LoadCardsAsync();
    }

    // ------------------------------------------------------------- 可导出的卡
    private async Task LoadCardsAsync()
    {
        if (App.Engine is not { } engine) return;
        try
        {
            var data = await engine.CallAsync("pack.list");
            _exportable.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("cards", out var cards) &&
                cards.ValueKind == JsonValueKind.Object)
            {
                foreach (var p in cards.EnumerateObject())
                {
                    if (!int.TryParse(p.Name, out var id)) continue;
                    var kinds = p.Value.TryGetProperty("kinds", out var k) &&
                                k.ValueKind == JsonValueKind.Array
                        ? k.EnumerateArray()
                             .Select(x => x.GetString() ?? "")
                             .Where(s => s.Length > 0)
                             .Select(KindText)
                             .ToList()
                        : new List<string>();
                    var name = p.Value.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                    _exportable.Add(new PackCardItem
                    {
                        Id = id,
                        Name = name,
                        Kinds = string.Join(" · ", kinds),
                        IsChecked = true,
                    });
                }
            }
            SetStatus(_exportable.Count > 0
                ? $"可导出 {_exportable.Count} 张卡"
                : "你还没有做过任何改动，没有可导出的卡");
            ExportEmpty.Visibility = _exportable.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            ExportList.Visibility = _exportable.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        }
        catch (Exception ex)
        {
            SetStatus("读取失败：" + ex.Message);
        }
    }

    private static string KindText(string kind) => kind switch
    {
        "cutin" => "召唤动画",
        "runfx" => "发动特效",
        "art" => "卡图",
        _ => kind,
    };

    // ------------------------------------------------------------------- 导出
    private async void ExportBtn_Click(object sender, RoutedEventArgs e)
    {
        var cards = _exportable.Where(c => c.IsChecked).Select(c => c.Id).ToList();
        if (cards.Count == 0)
        {
            await InfoAsync("没有要导出的卡", "先勾选至少一张卡。");
            return;
        }

        var folder = await PickFolderAsync();
        if (folder is null) return;

        var name = string.IsNullOrWhiteSpace(PackNameBox.Text) ? "my-pack" : PackNameBox.Text.Trim();
        var args = new Dictionary<string, object?>
        {
            ["cards"] = cards,
            ["out_path"] = folder,
            ["name"] = name,
            ["zip_it"] = FormatBox.SelectedIndex == 0,
        };

        var data = await RunJobAsync("pack.export", args, "正在导出…", "导出完成");
        if (data.ValueKind == JsonValueKind.Object &&
            data.TryGetProperty("path", out var p) && p.GetString() is { } path)
        {
            SetStatus("已导出到 " + path);
        }
    }

    // ------------------------------------------------------------------- 安装
    private async void InstallBtn_Click(object sender, RoutedEventArgs e)
    {
        var path = InstallPathBox.Text;
        if (string.IsNullOrWhiteSpace(path))
        {
            await InfoAsync("还没选整合包", "先选一个整合包文件或文件夹。");
            return;
        }
        if (!await ConfirmAsync("安装整合包", $"确定安装 {path} 吗？装的是别人做的数据。"))
            return;

        var args = new Dictionary<string, object?>
        {
            ["pack_path"] = path,
            ["skip_existing"] = SkipExistingBox.IsChecked == true,
            ["replace"] = ReplaceBox.IsChecked == true,
        };
        await RunJobAsync("pack.install", args, "正在安装…", "安装完成");
        await LoadCardsAsync();
    }

    private async void InstallBrowseBtn_Click(object sender, RoutedEventArgs e)
    {
        if (await PickPackFileAsync() is { } path) InstallPathBox.Text = path;
    }

    private async void InstallBrowseDir_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync() is { } path) InstallPathBox.Text = path;
    }

    // ------------------------------------------------------------------- 查看
    private async void InspectBtn_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        var path = InspectPathBox.Text;
        if (string.IsNullOrWhiteSpace(path))
        {
            await InfoAsync("还没选整合包", "先选一个整合包文件或文件夹。");
            return;
        }

        SetBusy(true);
        SetStatus("正在读取整合包…");
        try
        {
            var data = await engine.CallAsync("pack.inspect",
                new Dictionary<string, object?> { ["pack_path"] = path });
            _inspected.Clear();
            if (data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("cards", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var c in arr.EnumerateArray())
                {
                    // 一张卡的动画/特效/卡图合并成一行, 类型用「·」串起来
                    var kinds = (c.TryGetProperty("kind", out var k) ? k.GetString() ?? "" : "")
                        .Split(',', StringSplitOptions.RemoveEmptyEntries)
                        .Select(KindText);
                    _inspected.Add(new InspectItem
                    {
                        Card = c.TryGetProperty("mrk", out var m) ? m.ToString() : "",
                        Name = c.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
                        Kind = string.Join(" · ", kinds),
                    });
                }
            }
            var packName = data.TryGetProperty("name", out var pn) ? pn.GetString() ?? "" : "";
            var tool = data.TryGetProperty("tool", out var tl) ? tl.GetString() ?? "" : "";

            // 面板里留一行摘要，关掉弹窗也知道刚才看了什么
            InspectSummary.Visibility = Visibility.Visible;
            InspectSummary.Text = _inspected.Count > 0
                ? $"{packName}（{tool}）：{_inspected.Count} 张卡"
                : $"{packName}（{tool}）：里面没有卡";
            SetStatus("已读取 " + packName);

            await ShowInspectDialogAsync(packName, tool);
        }
        catch (Exception ex)
        {
            InspectSummary.Visibility = Visibility.Collapsed;
            await InfoAsync("读不了这个整合包", ex.Message);
        }
        finally
        {
            SetBusy(false);
        }
    }

    /// <summary>
    /// 把整合包内容列出来给用户看。
    /// 卡片多时在框内滚动（纵向滚动条），不撑爆窗口。
    /// </summary>
    private async Task ShowInspectDialogAsync(string packName, string tool)
    {
        var res = Application.Current.Resources;
        var strongStyle = (Style)res["BodyStrongTextBlockStyle"];
        var captionStyle = (Style)res["CaptionTextBlockStyle"];
        var secondary = (Brush)res["TextFillColorSecondaryBrush"];
        var frameStroke = (Brush)res["PanelBorderBrush"];
        var frameFill = (Brush)res["LayerFillColorAltBrush"];

        const double cardColWidth = 88;
        // 一张卡可能同时有动画+特效+卡图, 类型那一列要放得下三段文字
        const double kindColWidth = 190;

        var body = new StackPanel { Spacing = 14, MinWidth = 560 };

        // ---- 抬头：包名 + 工具与卡数 ----
        var head = new StackPanel { Spacing = 3 };
        head.Children.Add(new TextBlock
        {
            Text = string.IsNullOrWhiteSpace(packName) ? "未命名整合包" : packName,
            Style = strongStyle,
            TextWrapping = TextWrapping.Wrap,
        });
        var meta = new List<string>();
        if (!string.IsNullOrWhiteSpace(tool)) meta.Add(tool);
        meta.Add(_inspected.Count > 0 ? $"{_inspected.Count} 张卡" : "里面没有卡");
        // WinUI 的滚动条是悬停才展开的浮层, 不在交互时不占像素.
        // 卡多时用文字点明"能滚", 否则用户只看到一截列表, 以为内容不完整.
        if (_inspected.Count > 10) meta.Add("可上下滚动");
        head.Children.Add(new TextBlock
        {
            Text = string.Join(" · ", meta),
            Style = captionStyle,
            Foreground = secondary,
        });
        body.Children.Add(head);

        if (_inspected.Count > 0)
        {
            // ---- 表格：表头固定，内容在框内滚动 ----
            var table = new StackPanel { Spacing = 0 };

            Grid Row(string a, string b, string c, bool isHeader)
            {
                var g = new Grid
                {
                    ColumnSpacing = 12,
                    Padding = new Thickness(0, isHeader ? 0 : 5, 0, isHeader ? 0 : 5),
                };
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(cardColWidth) });
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(kindColWidth) });

                TextBlock Cell(string text, bool right)
                {
                    var tb = new TextBlock
                    {
                        Text = text,
                        VerticalAlignment = VerticalAlignment.Center,
                        TextTrimming = TextTrimming.CharacterEllipsis,
                    };
                    if (isHeader)
                    {
                        tb.Style = captionStyle;
                        tb.Foreground = secondary;
                    }
                    if (right) tb.HorizontalAlignment = HorizontalAlignment.Right;
                    return tb;
                }

                var c0 = Cell(a, false);
                var c1 = Cell(b, false);
                var c2 = Cell(c, true);
                Grid.SetColumn(c0, 0);
                Grid.SetColumn(c1, 1);
                Grid.SetColumn(c2, 2);
                g.Children.Add(c0);
                g.Children.Add(c1);
                g.Children.Add(c2);
                return g;
            }

            table.Children.Add(Row("卡号", "卡名", "类型", true));

            // 表头下的细分隔线
            table.Children.Add(new Border
            {
                Height = 1,
                Margin = new Thickness(0, 4, 0, 4),
                Background = secondary,
                Opacity = 0.25,
            });

            var rows = new StackPanel { Spacing = 0 };
            for (var i = 0; i < _inspected.Count; i++)
            {
                var it = _inspected[i];
                rows.Children.Add(Row(it.Card, it.Name, it.Kind, false));
                // 行之间淡淡的分隔，卡片多时不会糊成一片
                if (i < _inspected.Count - 1)
                {
                    rows.Children.Add(new Border
                    {
                        Height = 1,
                        Background = secondary,
                        Opacity = 0.10,
                    });
                }
            }

            table.Children.Add(new ScrollViewer
            {
                MaxHeight = 360,
                // 常显滚动条: 卡多时用户要能一眼看出下面还有, 而不是被截断
                VerticalScrollBarVisibility = ScrollBarVisibility.Visible,
                VerticalScrollMode = ScrollMode.Auto,
                Padding = new Thickness(0, 0, 4, 0),
                Content = rows,
            });

            body.Children.Add(new Border
            {
                Padding = new Thickness(14, 10, 14, 12),
                Background = frameFill,
                BorderBrush = frameStroke,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Child = table,
            });
        }

        var dlg = new ContentDialog
        {
            Title = "整合包内容",
            Content = body,
            CloseButtonText = "关闭",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = XamlRoot,
        };
        // 默认上限 548 太窄, 三列排不开
        dlg.Resources["ContentDialogMaxWidth"] = 860.0;
        await dlg.ShowAsync();
    }

    private async void InspectBrowseBtn_Click(object sender, RoutedEventArgs e)
    {
        if (await PickPackFileAsync() is { } path) InspectPathBox.Text = path;
    }

    private async void InspectBrowseDir_Click(object sender, RoutedEventArgs e)
    {
        if (await PickFolderAsync() is { } path) InspectPathBox.Text = path;
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

    private void SetBusy(bool busy)
    {
        ExportBtn.IsEnabled = !busy;
        InstallBtn.IsEnabled = !busy;
        InspectBtn.IsEnabled = !busy;
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
