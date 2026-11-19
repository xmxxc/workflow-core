using System;
using System.IO;
using System.Runtime.InteropServices;

class QueueProbe {
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    static int Main(string[] args) {
        if (args.Length != 6 || args[0] != "queue" || args[1] != "--thread" ||
            args[2] != Environment.GetEnvironmentVariable("CODEX_THREAD_ID") ||
            args[3] != "--message" || args[5] != "--resume") return 2;
        string path = Path.Combine(Environment.GetEnvironmentVariable("QUEUE_CAPTURE"), Guid.NewGuid().ToString());
        File.WriteAllLines(path + ".tmp", new string[] { GetConsoleWindow().ToInt64().ToString(), args[4] });
        File.Move(path + ".tmp", path + ".received");
        return 0;
    }
}
