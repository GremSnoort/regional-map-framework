"""Read generated XLSX independently of the JavaScript workbook writer."""
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Excel writer tests require Node.js; runtime server does not')
class TableExportTest(unittest.TestCase):
    def test_workbook_types_order_filters_and_metadata(self):
        module = Path(__file__).resolve().parents[1] / 'core/table-export.js'
        payload = {
            'headers': ['№', 'Населённый пункт', 'Население', 'Оценка'],
            'rows': [[1, 'Ольга & <край>', 10000, 1.25], [2, '=1+1', 0, None], [3, 'Неизвестно', 1.125, -2.5], [4, 'Нет данных', None, None]],
            'formats': ['number', None, 'number', 'decimal'],
            'metadata': [['Регион', 'Приморский край'], ['Источники', 'Строка 1\nСтрока 2'], ['Специальный текст', '_x0041_ / _x005F_ / \r / \x01']],
        }
        script = """
import fs from 'node:fs';
const source=fs.readFileSync(process.argv[1],'utf8');
const {tableWorkbook}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const blob=tableWorkbook(JSON.parse(fs.readFileSync(0,'utf8')));
fs.writeFileSync(process.argv[2],Buffer.from(await blob.arrayBuffer()));
"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'table.xlsx'
            subprocess.run(['node', '--input-type=module', '-e', script, str(module), str(output)], input=json.dumps(payload), text=True, check=True)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    ET.fromstring(archive.read(name))
                ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                cells = {c.attrib['r']: c for c in sheet.findall('.//s:c', ns)}
                self.assertEqual(cells['B2'].find('s:is/s:t', ns).text, 'Ольга & <край>')
                self.assertEqual(cells['B3'].attrib['t'], 'inlineStr')
                self.assertEqual(cells['B3'].find('s:is/s:t', ns).text, '=1+1')
                self.assertFalse(sheet.findall('.//s:f', ns))
                for ref, value in [('A2', '1'), ('C2', '10000'), ('C3', '0'), ('C4', '1.125'), ('D2', '1.25'), ('D4', '-2.5')]:
                    self.assertNotIn('t', cells[ref].attrib)
                    self.assertEqual(cells[ref].find('s:v', ns).text, value)
                self.assertIsNone(cells['D3'].find('s:v', ns))
                self.assertIsNone(cells['C5'].find('s:v', ns))
                self.assertEqual(sheet.find('s:autoFilter', ns).attrib['ref'], 'A1:D5')
                self.assertEqual(sheet.find('s:sheetViews/s:sheetView/s:pane', ns).attrib['topLeftCell'], 'A2')
                self.assertEqual(cells['D2'].attrib['s'], '3')
                styles = ET.fromstring(archive.read('xl/styles.xml'))
                formats = {node.attrib['numFmtId']: node.attrib['formatCode'] for node in styles.findall('s:numFmts/s:numFmt', ns)}
                xfs = styles.findall('s:cellXfs/s:xf', ns)
                self.assertEqual(formats[xfs[int(cells['C4'].attrib['s'])].attrib['numFmtId']], '#,##0.###')
                self.assertEqual(formats[xfs[int(cells['D2'].attrib['s'])].attrib['numFmtId']], '#,##0.##')
                info = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
                values = [c.text for c in info.findall('.//s:t', ns)]
                self.assertIn('Приморский край', values)
                self.assertIn('Строка 1\nСтрока 2', values)
                self.assertIn('_x005F_x0041_ / _x005F_x005F_ / _x000D_ / _x0001_', values)

    def test_long_text_is_rejected_instead_of_silently_truncated(self):
        module = Path(__file__).resolve().parents[1] / 'core/table-export.js'
        script = """
import fs from 'node:fs';
import assert from 'node:assert/strict';
const {tableWorkbook}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(process.argv[1],'utf8')).toString('base64'));
assert.throws(()=>tableWorkbook({headers:['Название'],rows:[['А'.repeat(32768)]]}),/32767/);
assert.doesNotThrow(()=>tableWorkbook({headers:['Название'],rows:[['А'.repeat(32767)]]}));
"""
        subprocess.run(['node', '--input-type=module', '-e', script, str(module)], check=True)

    def test_empty_table_retains_headers_and_valid_filter(self):
        module = Path(__file__).resolve().parents[1] / 'core/table-export.js'
        script = """
import fs from 'node:fs';
const {tableWorkbook}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(process.argv[1],'utf8')).toString('base64'));
const blob=tableWorkbook({headers:Array.from({length:28},(_,i)=>'Столбец '+i),rows:[]});
fs.writeFileSync(process.argv[2],Buffer.from(await blob.arrayBuffer()));
"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'empty.xlsx'
            subprocess.run(['node', '--input-type=module', '-e', script, str(module), str(output)], check=True)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                self.assertEqual(sheet.find('s:autoFilter', ns).attrib['ref'], 'A1:AB1')
                self.assertEqual(len(sheet.findall('s:sheetData/s:row', ns)), 1)
                self.assertEqual(sheet.findall('.//s:c', ns)[-1].attrib['r'], 'AB1')


if __name__ == '__main__':
    unittest.main()
