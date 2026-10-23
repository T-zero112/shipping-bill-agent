"""Explicit synthetic invoice; never mixed with company sample evaluation."""
from pathlib import Path
root=Path('outputs/financial/synthetic')
root.mkdir(parents=True,exist_ok=True)
(root/'invoice.txt').write_text('INVOICE DN-DEMO-001\nISSUED BY: Alpha LLC\nBILL TO: Beta LLC\nHandling Charge USD 40.00\nTOTAL USD 40.00\n',encoding='utf-8')
print('Synthetic invoice written; Alpha creditor, Beta debtor, no real-world parties.')
