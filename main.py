import argparse
from pathlib import Path

import customtkinter as ctk

from ctkpdfium import CTkPdfium


def main():
    parser = argparse.ArgumentParser(description='Preview a PDF with CTkPdfium')
    parser.add_argument('pdf', type=Path)
    args = parser.parse_args()
    root = ctk.CTk()
    root.geometry('800x600')
    pdf_frame = CTkPdfium(root, file=args.pdf, navigation_overlay=True,
                          on_error=lambda page, message: print(f'PDF error ({page}): {message}'))
    pdf_frame.pack(fill='both', expand=True, padx=10, pady=10)
    root.mainloop()


if __name__ == '__main__':
    main()
