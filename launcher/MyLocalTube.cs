// MyLocalTube.exe — アプリ版の起動用ランチャ。
//
// 自分と同じフォルダにある .venv\Scripts\pythonw.exe で desktop.py を起動して、すぐ終了する。
// 画面やサーバの本体は Python 側にあるので、Python/HTML を直してもこの exe は作り直さなくてよい。
// 作り直しが必要なのは、このファイルかアイコンを変えたときだけ（launcher\build.cmd）。
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("MyLocalTube")]
[assembly: AssemblyProduct("MyLocalTube")]
[assembly: AssemblyVersion("0.2.0.0")]

static class Launcher
{
    const string Title = "MyLocalTube";

    [STAThread]
    static int Main(string[] args)
    {
        string home = AppDomain.CurrentDomain.BaseDirectory;
        string py = Path.Combine(home, @".venv\Scripts\pythonw.exe");
        string script = Path.Combine(home, "desktop.py");

        if (!File.Exists(py) || !File.Exists(script))
        {
            MessageBox.Show(
                "起動に必要なファイルが見つかりません。\n\n" + py + "\n" + script +
                "\n\nMyLocalTube.exe は desktop.py と同じフォルダ（リポジトリの直下）に置き、先に README の手順で .venv を作ってください。" +
                "\n（別の場所から起動したい場合は、exe を動かさずショートカットを作ってください）",
                Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        var cmd = new StringBuilder();
        cmd.Append('"').Append(script).Append('"');
        foreach (string a in args)
            cmd.Append(" \"").Append(a.Replace("\"", "\\\"")).Append('"');

        var psi = new ProcessStartInfo(py, cmd.ToString());
        psi.WorkingDirectory = home;
        psi.UseShellExecute = false;
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            MessageBox.Show("起動できませんでした。\n\n" + e.Message, Title,
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }
}
