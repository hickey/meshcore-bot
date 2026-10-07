import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(
    new URL('../../modules/web_viewer/static/js/battery.js', import.meta.url),
    'utf8',
);
const helpers = source.slice(
    source.indexOf('    function formatVoltageTick'),
    source.indexOf('    function statusText'),
);
const context = vm.createContext({});
vm.runInContext(`${helpers}\nglobalThis.formatVoltageTick = formatVoltageTick;\nglobalThis.createTimeTickFormatter = createTimeTickFormatter;`, context);

test('voltage ticks use exactly two decimal places', () => {
    assert.equal(context.formatVoltageTick(3.7), '3.70');
    assert.equal(context.formatVoltageTick(4.125), '4.13');
});

test('time ticks prefix the first time for each local date', () => {
    const points = [
        { timestamp: '2026-10-07T09:15:00' },
        { timestamp: '2026-10-07T10:30:00' },
        { timestamp: '2026-10-08T00:05:00' },
    ];
    const format = context.createTimeTickFormatter(points);

    const first = format('', 0);
    const second = format('', 1);
    const third = format('', 2);
    const firstDate = new Date(points[0].timestamp);
    const thirdDate = new Date(points[2].timestamp);
    const expected = date => `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;

    assert.equal(first, expected(firstDate));
    assert.equal(second, `${String(new Date(points[1].timestamp).getHours()).padStart(2, '0')}:30`);
    assert.equal(third, expected(thirdDate));
});

test('time ticks fall back to invalid timestamp text', () => {
    const format = context.createTimeTickFormatter([{ timestamp: 'not-a-date' }]);
    assert.equal(format('', 0), 'not-a-date');
});
