# Bilibili 创作者平台 API 文档

Base URL: `https://member.bilibili.com/x2/creative/web`

## 公共说明

### 认证

所有请求需在 Header 中携带 `Cookie`，包含 `SESSDATA`、`bili_jct` 等认证字段。

### CSRF

写操作（POST）需携带 `csrf` 参数，值为 Cookie 中的 `bili_jct`。

### 通用响应格式

```json
{
  "code": 0,
  "message": "0",
  "ttl": 1,
  "data": {}
}
```

---

## 1. 获取合集信息

`GET /season`

### 参数

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| id | query | int | 合集 ID |

### 响应 data

```json
{
  "season": {
    "id": 7075988,
    "title": "合集标题",
    "desc": "合集描述",
    "cover": "封面URL",
    "isEnd": 0,
    "mid": 10815278,
    "is_pay": 0,
    "state": 0,
    "ctime": 1767251998,
    "mtime": 1775547558,
    "season_price": 0,
    "is_opened": 1
  },
  "sections": {
    "sections": [
      {
        "id": 7802233,
        "type": 1,
        "seasonId": 7075988,
        "title": "正片",
        "order": 1,
        "epCount": 170
      }
    ],
    "total": 2
  }
}
```

### 获取账号下的合集列表

`GET /seasons`

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| pn | query | int | 页码，从1开始 |
| ps | query | int | 每页数量 |

响应中的 `data.seasons` 为合集列表；每项的 `season.id` 和 `season.title` 分别为合集ID和标题。

### 获取账号最近投稿

Base URL：`https://member.bilibili.com/x/web`

`GET /archives`

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| status | query | string | `pubed` 表示已发布稿件 |
| pn | query | int | 页码，从1开始 |
| ps | query | int | 每页数量 |

响应中的 `data.arc_audits` 为投稿列表，稿件标题、BV号和发布时间位于每项的 `Archive` 对象。

---

## 2. 编辑合集

`POST /season/edit?csrf={csrf}`

### 请求体 (JSON)

```json
{
  "season": {
    "id": 7075988,
    "title": "合集标题",
    "desc": "合集描述",
    "cover": "封面URL",
    "isEnd": 0,
    "season_price": 0,
    "captcha_token": ""
  },
  "sorts": [
    { "id": 7802233, "sort": 1 },
    { "id": 8718287, "sort": 2 }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| season.id | int | 合集 ID |
| season.title | string | 合集标题 |
| season.desc | string | 合集描述 |
| season.cover | string | 封面 URL |
| season.isEnd | int | 是否完结（0=连载，1=完结） |
| sorts | array | section 排序，id 为 section ID |

---

## 3. 创建小节 (Section)

`POST /season/section/add?csrf={csrf}`

### 请求体 (JSON)

```json
{
  "type": 0,
  "seasonId": 7075988,
  "title": "二月",
  "captcha_token": ""
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| type | int | 小节类型（0=普通，1=正片） |
| seasonId | int | 所属合集 ID |
| title | string | 小节标题 |

### 响应

`data` 为新创建的 section ID（int）。

---

## 4. 编辑小节 (Section)

`POST /season/section/edit?csrf={csrf}`

### 请求体 (JSON)

```json
{
  "section": {
    "id": 7802233,
    "type": 1,
    "seasonId": 7075988,
    "title": "一月"
  },
  "sorts": [
    { "id": 167698133, "sort": 1 },
    { "id": 167800799, "sort": 2 }
  ],
  "captcha_token": ""
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| section.id | int | 小节 ID |
| section.title | string | 小节标题 |
| sorts | array | episode 排序，id 为 episode ID |

---

## 5. 删除小节 (Section)

`POST /season/section/del`

### 请求体 (application/x-www-form-urlencoded)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | 小节 ID |
| csrf | string | CSRF token |

---

## 6. 获取小节信息

`GET /season/section`

### 参数

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| id | query | int | 小节 ID |

### 响应 data

```json
{
  "section": {
    "id": 7802233,
    "type": 1,
    "seasonId": 7075988,
    "title": "一月",
    "order": 1,
    "epCount": 170
  },
  "episodes": [
    {
      "id": 167698133,
      "title": "2026年01月02日中午",
      "aid": 115824882881050,
      "bvid": "BV1X3iFBrEkt",
      "cid": 35142631829,
      "seasonId": 7075988,
      "sectionId": 7802233,
      "order": 1,
      "videoTitle": "01月02日12点00分",
      "archiveTitle": "羊羊不吃草-带弹幕回放2026年01月02日中午",
      "archiveState": 0,
      "state": 0
    }
  ]
}
```

---

## 7. 在Section中添加 Episode

`POST /season/section/episodes/add?csrf={csrf}`

### 请求体 (JSON)

```json
{
  "sectionId": 8718596,
  "episodes": [
    {
      "title": "04月04日下午",
      "aid": 116347275122675,
      "cid": 37242668657,
      "charging_pay": 0,
      "member_first": 0,
      "limited_free": false
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| sectionId | int | 目标小节 ID |
| episodes[].title | string | 剧集标题 |
| episodes[].aid | int | 稿件 AV 号 |
| episodes[].cid | int | 稿件 CID |

---

## 8. 移动 Episode

`POST /season/section/episode/move`

### 请求体 (application/x-www-form-urlencoded)

| 字段 | 类型 | 说明 |
|------|------|------|
| sectionId | int | 目标小节 ID |
| epId | int | Episode ID |
| csrf | string | CSRF token |

---

## 9. 删除 Episode

`POST /season/section/episode/del`

### 请求体 (application/x-www-form-urlencoded)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int | Episode ID |
| csrf | string | CSRF token |

---

## 10. 切换合集 Section 功能开关

`POST /season/section/switch`

切换合集是否启用小节（Section）功能。当 `no_section=1` 时，合集未启用小节功能，仅有一个默认的"正片" Section，此时无法调用创建 Section 的 API；需先调用本接口将 `no_section` 设为 `0` 以启用小节功能。

### 请求体 (application/x-www-form-urlencoded)

| 字段 | 类型 | 说明 |
|------|------|------|
| no_section | int | 小节功能开关（0=启用小节，1=禁用小节） |
| season_id | int | 合集 ID |
| csrf | string | CSRF token |
