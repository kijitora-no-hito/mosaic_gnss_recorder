# mosaic_gnss_recorder

Septentrio mosaic 受信機に接続して、測位状況と QZSS L6 の受信状況を見ながらログを保存するツールです。

## 起動

リポジトリのルートで実行します。

```
pip install -r requirements.txt
python -m mosaic_gnss_recorder
```

exe 版は `packaging/build.ps1` で作ります。
PyInstaller と Inno Setup 6 が必要で、インストーラは `packaging/Output/` にできます。
インストールするとスタートメニューに mosaic_gnss_recorder が追加されます。管理者権限は不要です。
exe 版の既定の保存先は「ドキュメント\\mosaic_gnss_recorder」です。

Septentrio の USB ドライバは同梱していません。RxTools を USB Driver 付きでインストールしてください。
ドライバが無い場合、インストーラが案内を表示します。

## 使い方

1. ポートで **Virtual USB COM Port 1 または 2** を選び、[接続] を押す。
   機種と FW を自動で読み取るだけで、受信機の設定は変えない
2. [受信設定を投入] を押す。送るコマンドは次の 2 つ。Current だけを変更し、Boot には保存しない
   ```
   setSignalTracking,+QZSL6
   setSBFOutput,Stream10,<接続中のポート>,Rinex+PVTGeod+Status+GALRawCNAV+QZSRawL6D+QZSRawL6E,sec1
   ```
   L6 に対応しない受信機(Capabilities に QZSL6 が無いもの)には、L6 に関する部分を除いて送る
3. 保存先を確認して [記録開始] を押す
4. 終わったら [記録停止]、[SBF出力を停止]、[切断] の順に押す

電源を入れ直すと、受信機は Boot の設定に戻ります。

右下の欄からは、任意のコマンドを送れます(`getPVTMode` など)。

## 保存されるファイル

| ファイル | 内容 |
|---|---|
| `mosaic_日時.sbf` | CRC 検査を通った SBF ブロック。sbf2rin で RINEX に変換できる |
| `mosaic_日時_pvt.csv` | 測位結果(測位モード、緯度経度、楕円体高、速度、衛星数、推定精度)。**位置情報を含む** |
| `mosaic_日時_L6D_Jnn.l6` | CLAS 用。RS 復号に成功したメッセージだけを 250 バイト単位で並べたもの |
| `mosaic_日時_L6E_Jnn.l6` | MADOCA-PPP 用。形式は同じ |

既存のファイルは上書きしません。

## 実機なしで試す

[SBF再生] で記録済みの SBF を開くと、速度を選んで再生できます。
再生中に [記録開始] を押すと、SBF から .l6 を作れます。

## 表示

- 測位: 測位モード(色付きで大きく表示)、使用衛星数、推定精度、速度、受信ブロック数
- QZSS 衛星: 衛星ごとの仰角と L1CA / L2C / L5 / L6 の C/N0。「—」は未追尾
- L6 メッセージ: チャネル・衛星ごとの受信数、RS 復号の成功率、訂正シンボル数の平均
- 信号別の追尾数: 特定の帯域だけ受からない状況の切り分けに使う
- 下段のタブ:
  - QZSS C/N0: L6 と L1CA の時系列
  - 測位履歴: 測位モード、位置の変化(平均からの差)、推定精度、使用衛星数の時系列
  - スカイプロット: 追尾中の衛星の配置(探索中のチャネルは出さない)
  - 全衛星: 衛星ごとの方位・仰角と、帯域ごとの C/N0
- グラフの時間幅: タブ右上で 1 分 / 5 分 / 15 分 / 1 時間 / 全期間を選ぶ。
  全期間以外は右端が現在で、線が左へ流れる。全期間は記録開始からの経過時間で表示する
- 赤字: QZSL6 が無い受信機、観測量のスクランブル、L6 未受信

## 緯度経度と地図

「表示」メニューで切り替えます。起動時はどちらも OFF です。

- 緯度経度を表示: 測位欄に緯度、経度、楕円体高を出す
- 地図を表示: 地図タブを追加し、現在位置と軌跡を OpenStreetMap に重ねる

地図を ON にすると、Leaflet(unpkg.com)と地図タイル(tile.openstreetmap.org)をインターネットから取得します。
タイルの要求から、表示している場所の概略が地図サーバに伝わります。OFF の間は接続しません。

## 構成

| ファイル | 役割 |
|---|---|
| `sbf_parser.py` | SBF の分離(CRC 検査)とデコード |
| `state.py` | 集計 |
| `logger.py` | ファイル保存 |
| `serial_link.py` | COM ポートの入出力、再生、コマンド送信 |
| `charts.py` | グラフとスカイプロット |
| `map_view.py` | 地図 |
| `app.py` | GUI |

テストは `test/test_mosaic_gnss_recorder.py` です。
