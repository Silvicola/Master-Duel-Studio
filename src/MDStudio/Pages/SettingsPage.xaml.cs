using System.Text.Json;
using MDStudio.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using Windows.Storage.Pickers;

namespace MDStudio.Pages;

/// <summary>账号下拉项：id 是目录名，label 是「哈希 · 备注名」。</summary>
public sealed record AccountOption(string Id, string Label);

public sealed partial class SettingsPage : Page
{
    private bool _loading;

    public SettingsPage()
    {
        InitializeComponent();
        SettingsScroller.SizeChanged += (_, _) => SyncBodyWidth();
        Unloaded += (_, _) => App.DryRunChanged -= OnDryRunChanged;
    }

    /// <summary>标题栏徽标关掉预演时，把这里的开关同步过来。</summary>
    private void OnDryRunChanged()
    {
        if (DrySwitch.IsOn != App.DryRun) DrySwitch.IsOn = App.DryRun;
    }

    /// <summary>把内容宽度钉到视口宽度（上限 820），否则 ScrollViewer 无限宽测量会让 * 列塌成内容宽。</summary>
    private void SyncBodyWidth()
    {
        var w = SettingsScroller.ActualWidth;
        if (double.IsNaN(w) || w <= 0) return;
        var target = Math.Min(w, 820);
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
        await LoadAsync();
    }

    private async Task LoadAsync()
    {
        if (App.Engine is not { } engine)
        {
            GameHint.Text = "引擎没有启动：" + (App.EngineError ?? "未知原因");
            return;
        }

        _loading = true;
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
            AccountBox.SelectedItem = opts.FirstOrDefault(o => o.Id == current) ?? opts.FirstOrDefault();
            NameBox.Text = NicknameOf(AccountBox.SelectedItem as AccountOption);

            GameHint.Text = opts.Count > 1
                ? "检测到多个账号。两个账号的游戏文件是两份独立副本，切换账号等于切换历史。"
                : "";

            var info = await engine.CallAsync("info");
            VersionText.Text = Str(info, "version");
            JournalText.Text = Str(info, "journal");
            RecordsText.Text = info.TryGetProperty("records", out var r) ? r.GetInt32().ToString() : "—";
        }
        catch (Exception ex)
        {
            GameHint.Text = "读取设置失败：" + ex.Message;
        }
        finally
        {
            _loading = false;
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
        }
        catch (Exception ex)
        {
            GameHint.Text = "切换账号失败：" + ex.Message;
        }
    }

    private async void SaveName_Click(object sender, RoutedEventArgs e)
    {
        if (AccountBox.SelectedItem is not AccountOption opt || App.Engine is not { } engine) return;
        try
        {
            // 存完直接重读一遍，别在这里手工改下拉里的项
            await engine.CallAsync("settings.set", new { account = opt.Id, name = NameBox.Text.Trim() });
            GameHint.Text = "备注名已保存";
        }
        catch (Exception ex)
        {
            GameHint.Text = "保存失败：" + ex.Message;
        }
        await LoadAsync();
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

        try
        {
            await engine.CallAsync("settings.set", new { game_dir = folder.Path });
            GameHint.Text = "已切换游戏目录";
        }
        catch (Exception ex)
        {
            GameHint.Text = "切换失败：" + ex.Message;
        }
        await LoadAsync();
    }

    private async void Detect_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        try
        {
            var s = await engine.CallAsync("settings.get");
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
            await InfoAsync("重新同步失败", ex.Message);
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

        // broken 的键是卡号，值是缺的依赖名。按卡聚起来才是用户能看懂的单位，
        // 逐个文件报 88 条 hash 路径没人看得下去。
        var broken = new List<string>();
        if (r.TryGetProperty("broken", out var b) && b.ValueKind == JsonValueKind.Object)
            broken.AddRange(b.EnumerateObject().Select(p => p.Name));

        if (cutins.Count == 0 && fx.Count == 0 && fixes == 0 && moved == 0 && lost.Count == 0)
        {
            ResyncStatus.Text = "没有需要补的，游戏清单和历史现在一致。";
            return;
        }

        var head = $"补回召唤动画 {cutins.Count} 处、发动特效 {fx.Count} 处，修订清单 {fixes} 项";
        if (moved > 0) head += $"，搬移文件 {moved} 个";
        ResyncStatus.Text = App.DryRun ? head + "（预演，未写入）" : head;

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

        if (todo.Count == 0) return;
        await InfoAsync("同步完成，有几处需要处理", string.Join("\n", todo));
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

    private async Task InfoAsync(string title, string body)
    {
        var dlg = new ContentDialog
        {
            Title = title,
            Content = new TextBlock { Text = body, TextWrapping = TextWrapping.Wrap },
            CloseButtonText = "好",
            XamlRoot = XamlRoot,
        };
        await dlg.ShowAsync();
    }

    // --------------------------------------------------------------- 预演
    private void DrySwitch_Toggled(object sender, RoutedEventArgs e)
    {
        App.DryRun = DrySwitch.IsOn;
        (App.MainWindow as MainWindow)?.UpdateDryRunBadge();
    }
}
