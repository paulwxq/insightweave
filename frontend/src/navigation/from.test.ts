/**
 * doc 9 §7 来源（from）工具的单元测试。
 * 运行：node --test src/navigation/from.test.ts（在 frontend/ 下）。
 * 覆盖：§7.2 白名单与校验、嵌套内层剥离、notice 剔除；
 * §7.4 各页认可矩阵；§7.1 文案；§7.6 创建工作区来源合成；query 对象写回。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  resolveFrom,
  buildFrom,
  resolveBack,
  withFrom,
  appendNotice,
  synthesizeWorkspaceCreatedFrom,
  passThroughFrom,
  hierarchyFrom,
  crumbFrom,
  workspacesCrumbTo,
} from './from.ts'

// ---- §7.2 白名单与校验 ----

test('resolveFrom 接受五种白名单形式', () => {
  assert.equal(resolveFrom('/data-sources')?.kind, 'data-sources')
  assert.equal(resolveFrom('/data-sources?db_type=pg&q=a')?.kind, 'data-sources')
  assert.equal(resolveFrom('/data-sources/db_src_16')?.kind, 'data-sources-detail')
  assert.equal(resolveFrom('/workspaces?source_id=db_src_16&page=2')?.kind, 'workspaces')
  assert.equal(resolveFrom('/collection-jobs?source_id=x&page=3')?.kind, 'collection-jobs')
  assert.equal(resolveFrom('/publish-jobs')?.kind, 'publish-jobs')
})

test('resolveFrom 拒绝站外、协议相对、非白名单与结构非法值', () => {
  for (const bad of [
    '',
    'workspaces',
    '//example.com',
    'https://example.com/x',
    '/query',
    '/workspaces/ws_68',
    '/workspaces/',
    '/data-sources/db_src_16/jobs',
    '/data-sources/',
    '/data-sources/db_src_16?q=1',
    '/data-sources?from=/collection-jobs',
    '/collection-jobs?from=/data-sources',
    '/publish-jobs?from=/x',
  ]) {
    assert.equal(resolveFrom(bad), null, `应拒绝: ${bad}`)
  }
  // §7.2 嵌套：内层不合法只丢内层，外层仍有效
  assert.equal(resolveFrom('/workspaces?from=x')?.target, '/workspaces')
})

test('resolveFrom 拒绝反斜杠、控制字符与超长值', () => {
  assert.equal(resolveFrom('/workspaces\\x'), null)
  assert.equal(resolveFrom('/workspaces?\x01'), null)
  assert.equal(resolveFrom(`/workspaces?q=${'a'.repeat(2100)}`), null)
})

test('resolveFrom 剥离不合法的内层 from，保留工作区列表地址', () => {
  const raw = '/workspaces?source_id=db_src_16&from=%2Fcollection-jobs%3Fq%3Dx'
  const got = resolveFrom(raw)
  assert.equal(got?.kind, 'workspaces')
  assert.equal(got?.target, '/workspaces?source_id=db_src_16')
})

test('resolveFrom 保留合法的内层 from（数据源形式）', () => {
  const raw = '/workspaces?source_id=db_src_16&from=%2Fdata-sources%2Fdb_src_16'
  const got = resolveFrom(raw)
  const inner = new URLSearchParams(got!.target.split('?')[1] ?? '')
  assert.equal(inner.get('from'), '/data-sources/db_src_16')
})

test('resolveFrom 内层为工作区地址（超一层）时只丢内层', () => {
  const raw = '/workspaces?from=%2Fworkspaces%3Fsource_id%3Da'
  assert.equal(resolveFrom(raw)?.target, '/workspaces')
})

// ---- §7.2 新建来源：剔除 notice，保留其余 ----

test('buildFrom 剔除 notice 保留其他参数', () => {
  assert.equal(buildFrom('/collection-jobs?q=订单&page=2'), '/collection-jobs?q=订单&page=2')
  assert.equal(buildFrom('/workspaces?source_id=a&notice=not_found&page=3'), '/workspaces?source_id=a&page=3')
  assert.equal(buildFrom('/workspaces?notice=not_found'), '/workspaces')
})

// ---- §7.4 各页认可矩阵 + §7.1 文案 ----

test('工作区列表：认数据源来源，否则无返回按钮', () => {
  assert.deepEqual(resolveBack('workspaces-list', '/data-sources?q=pg', {}), {
    target: '/data-sources?q=pg', label: '返回', fromTarget: true,
  })
  assert.deepEqual(resolveBack('workspaces-list', '/data-sources/db_src_16', {}), {
    target: '/data-sources/db_src_16', label: '返回', fromTarget: true,
  })
  assert.equal(resolveBack('workspaces-list', null, {}).target, null)
  assert.equal(resolveBack('workspaces-list', '/collection-jobs', {}).target, null)
})

test('工作区详情：认工作区列表来源且文案「返回」，默认 /workspaces', () => {
  assert.deepEqual(resolveBack('workspace-home', '/workspaces?source_id=a', {}), {
    target: '/workspaces?source_id=a', label: '返回', fromTarget: true,
  })
  assert.deepEqual(resolveBack('workspace-home', null, {}), {
    target: '/workspaces', label: '返回', fromTarget: false,
  })
  assert.equal(resolveBack('workspace-home', '/collection-jobs', {}).fromTarget, false)
})

test('新建工作区：认三种来源；默认按表单选择筛选列表', () => {
  assert.deepEqual(resolveBack('workspace-new', '/workspaces?page=2', {}), {
    target: '/workspaces?page=2', label: '返回', fromTarget: true,
  })
  assert.deepEqual(resolveBack('workspace-new', '/data-sources/db_src_16', {}), {
    target: '/data-sources/db_src_16', label: '返回', fromTarget: true,
  })
  assert.deepEqual(
    resolveBack('workspace-new', null, { selectedSourceId: 'db_src_16' }),
    { target: '/workspaces?source_id=db_src_16', label: '返回', fromTarget: false },
  )
  assert.deepEqual(
    resolveBack('workspace-new', null, {}),
    { target: '/workspaces', label: '返回', fromTarget: false },
  )
})

test('采集作业详情：认采集全局列表，不认发布全局列表；默认区内列表', () => {
  assert.deepEqual(
    resolveBack('collection-job-detail', '/collection-jobs?page=2', { workspaceId: 'ws_68' }),
    { target: '/collection-jobs?page=2', label: '返回', fromTarget: true },
  )
  const wrong = resolveBack('collection-job-detail', '/publish-jobs', { workspaceId: 'ws_68' })
  assert.equal(wrong.fromTarget, false)
  assert.deepEqual(
    resolveBack('collection-job-detail', null, { workspaceId: 'ws_68' }),
    { target: '/workspaces/ws_68/collection', label: '返回', fromTarget: false },
  )
})

test('发布作业详情对称；新建作业同详情；执行页不认 from 默认作业详情', () => {
  assert.deepEqual(
    resolveBack('publish-job-detail', '/publish-jobs?q=x', { workspaceId: 'ws_68' }),
    { target: '/publish-jobs?q=x', label: '返回', fromTarget: true },
  )
  assert.deepEqual(
    resolveBack('publish-job-new', null, { workspaceId: 'ws_68' }),
    { target: '/workspaces/ws_68/publish', label: '返回', fromTarget: false },
  )
  assert.deepEqual(
    resolveBack('run-execute', '/collection-jobs', { workspaceId: 'ws_68', jobId: 'j1', jobKind: 'collection' }),
    { target: '/workspaces/ws_68/collection/jobs/j1', label: '返回', fromTarget: false },
  )
})

test('数据资产与数据源页面不认 from，默认目标各异', () => {
  assert.deepEqual(resolveBack('database-profile', '/collection-jobs', { sourceId: 's1' }),
    { target: '/data-assets', label: '返回', fromTarget: false })
  assert.deepEqual(resolveBack('object-list', null, { sourceId: 's1' }),
    { target: '/data-assets/s1/profile', label: '返回', fromTarget: false })
  assert.deepEqual(resolveBack('object-detail', null, { sourceId: 's1', schemaName: 'public' }),
    { target: '/data-assets/s1/objects?schema_name=public', label: '返回', fromTarget: false })
  assert.deepEqual(resolveBack('object-detail', null, { sourceId: 's1' }),
    { target: '/data-assets/s1/objects', label: '返回', fromTarget: false })
  assert.deepEqual(resolveBack('source-detail', '/collection-jobs', {}),
    { target: '/data-sources', label: '返回', fromTarget: false })
  assert.deepEqual(resolveBack('source-new', null, {}),
    { target: '/data-sources', label: '返回', fromTarget: false })
  assert.deepEqual(resolveBack('source-edit', null, { sourceId: 's1' }),
    { target: '/data-sources/s1', label: '返回', fromTarget: false })
})

// ---- §7.6 创建工作区后的来源合成 ----

test('合成规则按四种情况顺序判断', () => {
  // 1. /workspaces… 保持原样
  assert.equal(synthesizeWorkspaceCreatedFrom('/workspaces?page=2', 's1'), '/workspaces?page=2')
  // 2. 数据源列表 / 同源详情 → 嵌套保留
  const v2 = synthesizeWorkspaceCreatedFrom('/data-sources?q=pg', 's1')!
  const [p2, q2] = splitQuery(v2)
  assert.equal(p2, '/workspaces')
  assert.equal(new URLSearchParams(q2).get('source_id'), 's1')
  assert.equal(new URLSearchParams(q2).get('from'), '/data-sources?q=pg')
  const v2b = synthesizeWorkspaceCreatedFrom('/data-sources/s1', 's1')!
  assert.equal(new URLSearchParams(splitQuery(v2b)[1]).get('from'), '/data-sources/s1')
  // 3. 异源详情 → 不保留嵌套
  const v3 = synthesizeWorkspaceCreatedFrom('/data-sources/s2', 's1')!
  assert.equal(v3, '/workspaces?source_id=s1')
  // 4. 无来源 / 不认 → null
  assert.equal(synthesizeWorkspaceCreatedFrom(null, 's1'), null)
  assert.equal(synthesizeWorkspaceCreatedFrom('/collection-jobs', 's1'), null)
})

// ---- query 对象写回 ----

test('withFrom 通过 query 对象附加来源，不破坏目标自身参数', () => {
  assert.equal(withFrom('/workspaces/ws_68', '/collection-jobs?page=2'),
    '/workspaces/ws_68?from=%2Fcollection-jobs%3Fpage%3D2')
  assert.equal(withFrom('/workspaces?source_id=a', null), '/workspaces?source_id=a')
  const nested = withFrom('/workspaces', withFrom('/data-sources', '/collection-jobs')!)
  assert.equal(nested, '/workspaces?from=%2Fdata-sources%3Ffrom%3D%252Fcollection-jobs')
})

test('appendNotice 在目标自身 query 之外追加提示', () => {
  assert.equal(appendNotice('/workspaces?source_id=a', 'not_found'),
    '/workspaces?source_id=a&notice=not_found')
  assert.equal(appendNotice('/workspaces', 'not_found'), '/workspaces?notice=not_found')
})

// ---- §7.3 传播：下钻原样传递 / 层级跳转仅留工作区来源 ----

test('passThroughFrom：有效值净化后传递，无效值不传', () => {
  assert.equal(passThroughFrom('/collection-jobs?page=2'), '/collection-jobs?page=2')
  // 内层不合法已被剥离的净化值
  assert.equal(passThroughFrom('/workspaces?source_id=a&from=bad'), '/workspaces?source_id=a')
  assert.equal(passThroughFrom('https://evil.example'), null)
  assert.equal(passThroughFrom(null), null)
})

test('hierarchyFrom：仅保留工作区列表来源，其余丢弃', () => {
  assert.equal(hierarchyFrom('/workspaces?source_id=a&page=2'), '/workspaces?source_id=a&page=2')
  assert.equal(hierarchyFrom('/collection-jobs?page=2'), null)
  assert.equal(hierarchyFrom('/data-sources/db_src_16'), null)
  assert.equal(hierarchyFrom('//example.com'), null)
})

// ---- §6.4/§7.3-3 路径段的链接来源 ----

test('crumbFrom：与返回目标同页原样带净化来源，否则仅工作区来源', () => {
  const listPath = '/workspaces/ws_68/collection'
  // 作业详情（默认返回=区内列表）：「采集作业」段与返回同页 → 原样带
  assert.equal(
    crumbFrom('/workspaces/ws_68/collection', listPath, '/collection-jobs?page=2'),
    '/collection-jobs?page=2',
  )
  // 返回目标取自全局 from 时与段目标不同页 → 仅工作区来源
  assert.equal(
    crumbFrom('/collection-jobs?page=2', listPath, '/collection-jobs?page=2'),
    null,
  )
  // 同页但来源为工作区列表 → 保留工作区来源
  assert.equal(
    crumbFrom(listPath, listPath, '/workspaces?source_id=a'),
    '/workspaces?source_id=a',
  )
  // 返回目标为 null（无按钮）→ 按不同页处理
  assert.equal(crumbFrom(null, listPath, '/collection-jobs'), null)
})

test('workspacesCrumbTo：from 指向工作区列表时恢复该地址，否则 /workspaces', () => {
  assert.equal(workspacesCrumbTo('/workspaces?source_id=a&page=2'), '/workspaces?source_id=a&page=2')
  assert.equal(workspacesCrumbTo('/collection-jobs'), '/workspaces')
  assert.equal(workspacesCrumbTo(null), '/workspaces')
})

function splitQuery(value: string): [string, string] {
  const idx = value.indexOf('?')
  return idx === -1 ? [value, ''] : [value.slice(0, idx), value.slice(idx + 1)]
}
