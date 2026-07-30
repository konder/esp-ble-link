# Bundle ID 登记表

每个 BLE helper 必须有独立的 `CFBundleIdentifier`。复用会让 LaunchServices 把两个
app 当成同一个 —— 启动其中一个,另一个就再也起不来,而且报错毫无提示性。
详见 [pitfalls B3](pitfalls.md#b3)。

`build_helper.sh` 会用 `mdfind` 查一遍本机有没有撞车,撞了直接拒绝构建。
但 `mdfind` 只看得到 Spotlight 索引过的路径,所以这份表还是有用的。

## 已知占用

| Bundle ID | 归属 | 备注 |
|---|---|---|
| `com.charlex.codebuddy.blehelper` | CharlexH/CodeBuddy(上游) | 别用 |
| `com.example.espble.echo.blehelper` | 本仓库 `examples/echo` 的示例值 | **示例专用,自己的项目请换** |

## 建议的命名

```
com.<你的名字或组织>.<设备名>.blehelper
```

新建了 helper 就往上面的表里加一行。这份表就是靠人维护的 —— 它拦不住所有撞车,
但能拦住最常见的那种(照着别人的项目抄配置时顺手把 bundle id 也抄了)。
