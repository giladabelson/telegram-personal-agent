# whisper.cpp Hebrew benchmark

Machine: 4-core/8-thread laptop CPU, no GPU, Windows 11. whisper.cpp build b5454 (CPU, plain and OpenBLAS). Model: ivrit-ai/whisper-large-v3-turbo-ggml (f16 original, q8_0, q5_0 made with whisper-quantize). Audio: bench.ogg, 29.7 s Hebrew, converted to 16 kHz mono WAV. Wall-clock via Measure-Command, one run per config (fastest config re-run twice more: 41.9 s and 52.4 s). Baseline: faster-whisper int8 CPU = 27 s.

Reference: "היי ג'רוויס, תזכיר לי מחר בשלוש להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגיע מייל מהאוניברסיטה לגבי המבחן בקוונטים, ותוסיף משימה לקנות חלב ולחם..."

| binary | model | threads | timestamps | seconds | first 80 chars |
|---|---|---|---|---|---|
| plain | f16 | 4 | on | 213 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | f16 | 8 | on | 144.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | f16 | 4 | off (--no-timestamps) | 115.7 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | f16 | 8 | off (--no-timestamps) | 73.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q8_0 | 4 | on | 168 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q8_0 | 8 | on | 113.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q8_0 | 4 | off (--no-timestamps) | 62.5 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q8_0 | 8 | off (--no-timestamps) | 54.6 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q5_0 | 4 | on | 163.7 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q5_0 | 8 | on | 124.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q5_0 | 4 | off (--no-timestamps) | 81.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| plain | q5_0 | 8 | off (--no-timestamps) | 63.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | f16 | 4 | on | 85 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | f16 | 8 | on | 88.1 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | f16 | 4 | off (--no-timestamps) | 51.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | f16 | 8 | off (--no-timestamps) | 43.7 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q8_0 | 4 | on | 92.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q8_0 | 8 | on | 65.6 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q8_0 | 4 | off (--no-timestamps) | 67.5 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q8_0 | 8 | off (--no-timestamps) | 48.2 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q5_0 | 4 | on | 85.6 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q5_0 | 8 | on | 73 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q5_0 | 4 | off (--no-timestamps) | 47.8 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |
| blas | q5_0 | 8 | off (--no-timestamps) | 44.4 | היי גרביץ, תזכיר לי מחר ב-3 להגיש את התרגיל בפיזיקה למרצה. חוץ מזה, תבדוק אם הגי |

Notes:
- No run beat the 27 s faster-whisper baseline; best was 41.9-52.4 s (blas, f16, -t 8, --no-timestamps).
- Runs with timestamps on were much slower than --no-timestamps (e.g. plain f16 -t 4: 213 s vs 115.7 s).
- Only 2 distinct transcripts across all 24 runs, differing by one word ("ותגיד" vs "תגיד"). Errors vs reference: "ג'רוויס" heard as "גרביץ", "בשלוש" as "ב-3", "בקוונטים" as "בכוונתים".
- Timings are single runs on a laptop and noisy (the first run also loaded the model from cold disk).

Fastest command line:

```
./whispercpp/bin_blas/Release/whisper-cli.exe -m ./whispercpp/model/ggml-model.bin -f ./whispercpp/audio/bench.wav -l he -t 8 --no-timestamps
```
