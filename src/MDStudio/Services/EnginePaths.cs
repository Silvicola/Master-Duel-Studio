namespace MDStudio.Services;

/// <summary>
/// 引擎文件的位置。引擎脚本随程序复制到输出目录的 engine/ 下。
///
/// 打包后的目录结构（便携版）：
///   程序目录\engine   引擎脚本（mdanim_engine）
///   程序目录\core     mdstudio 后端源码
///   程序目录\.pyenv   启动器建的 Python 环境
/// 开发期后两个都不在，就走固定路径。环境变量可以覆盖，方便换环境调试。
/// </summary>
public static class EnginePaths
{
    // 开发期用的路径：引擎源码在本工程 core/mdstudio，Python 环境放 .venv。
    // 跑便携版不需要这些；只有从 bin/Debug 直接启动时才走这里。
    // 不写死绝对路径：开源后每个人的工程放在哪都不一样。从程序集位置向上找
    // 含 core/mdstudio 的那一层，找不到就返回 null，用下面的兜底。
    private static readonly string? DevRoot = FindDevRoot();

    private static string? FindDevRoot()
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            if (Directory.Exists(Path.Combine(dir.FullName, "core", "mdstudio")))
                return dir.FullName;
        }
        return null;
    }

    private static readonly string? DevPython = DevRoot is null
        ? null
        : Path.Combine(DevRoot, ".venv", "Scripts", "python.exe");

    private static readonly string? DevCore = DevRoot is null
        ? null
        : Path.Combine(DevRoot, "core");

    public static string EngineDir => Path.Combine(AppContext.BaseDirectory, "engine");

    public static string Python =>
        Pick("MDANIM_PYTHON", BundledPython ?? PortablePython) ?? DevPython ?? "python.exe";

    public static string Core =>
        Pick("MDANIM_CORE", PortableCore) ?? DevCore
        ?? Path.Combine(AppContext.BaseDirectory, "core");

    /// <summary>在便携版目录里跑（core 在旁边）。用来判断该怎么提示用户。</summary>
    public static bool InPortableLayout => PortableCore is not null;

    /// <summary>便携包里自带的嵌入式 Python（免安装版）。没有就返回 null。</summary>
    private static string? BundledPython
    {
        get
        {
            var p = Path.Combine(AppContext.BaseDirectory, "python", "python.exe");
            return File.Exists(p) ? p : null;
        }
    }

    /// <summary>便携包里启动器建的 Python。没有就返回 null。</summary>
    private static string? PortablePython
    {
        get
        {
            var p = Path.Combine(AppContext.BaseDirectory, ".pyenv", "Scripts", "python.exe");
            return File.Exists(p) ? p : null;
        }
    }

    /// <summary>便携包里的 mdstudio 后端源码目录。</summary>
    private static string? PortableCore
    {
        get
        {
            var p = Path.Combine(AppContext.BaseDirectory, "core");
            return Directory.Exists(p) ? p : null;
        }
    }

    private static string? Pick(string env, string? fallback)
    {
        var v = Environment.GetEnvironmentVariable(env);
        if (!string.IsNullOrWhiteSpace(v)) return v;
        return fallback;
    }
}
