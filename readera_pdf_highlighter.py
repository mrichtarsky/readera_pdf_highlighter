from dataclasses import dataclass
from functools import cache
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZipFile

import fitz

@dataclass
class BookInfo:
    title: str
    filename: str
    citations: set

def get_all_citations(readera_backup_filename):
    uris_to_book_infos = {}
    print(f"Reading {readera_backup_filename}")
    with ZipFile(readera_backup_filename) as backup_file:
        with backup_file.open('library.json') as f:
            data = json.load(f)

    for doc in data['docs']:
        uri = doc['uri']
        doc_data = doc['data']
        try:
            book_title = doc_data['doc_title']
        except KeyError:
            book_title = doc_data['doc_file_name_title']
        doc_links = doc['links']
        assert len(doc_links) <= 1, doc_links
        try:
            book_filename = doc_links[0]['file_name']
        except IndexError:
            book_filename = None
        book_info = BookInfo(book_title, book_filename, set())
        uris_to_book_infos[uri] = book_info
        for citation in doc['citations']:
            try:
                note_extra = citation['note_extra']
            except KeyError:
                note_extra = None
            book_info.citations.add((citation['note_body'], citation['note_page']+citation['note_index'], note_extra))

    return uris_to_book_infos

def verify_citations_complete():
    """
    Check all ReadEra backup files in the current dir. Collect all citations.
    For each backup file, show whether it has all citations or which are missing.
    """
    all_uris_to_book_infos = {}
    readera_backups_to_map = {}
    for readera_backup_filename in sorted(Path('.').glob('ReadEra*bak'), reverse=True):
        uris_to_book_infos = get_all_citations(readera_backup_filename)
        readera_backups_to_map[readera_backup_filename] = uris_to_book_infos
        for uri, book_info in uris_to_book_infos.items():
            try:
                all_uris_to_book_infos[uri].citations.update(book_info.citations)
            except KeyError:
                all_uris_to_book_infos[uri] = book_info

    for readera_backup_filename, uris_to_book_infos in readera_backups_to_map.items():
        print(f"Checking {readera_backup_filename}")
        if uris_to_book_infos == all_uris_to_book_infos:
            print(f"  Contains all citations")
        else:
            for all_uri, all_book_info in all_uris_to_book_infos.items():
                try:
                    book_citations = uris_to_book_infos[all_uri].citations
                    for citation in all_book_info:
                        if citation not in book_citations:
                            print(f"  Missing citation: {all_book_info.title} / {citation}")
                except KeyError:
                    print(f"  Missing title {all_book_info.title}")
                    raise

def show_titles(readera_backup_filename):
    uris_to_book_infos = get_all_citations(readera_backup_filename)
    for book_info in sorted(uris_to_book_infos.values(), key=lambda book_info: book_info.title):
        if len(book_info.citations) > 0:
            print(f"{book_info.title}, Citations: {len(book_info.citations)}")

def get_citations_by_file(readera_backup_filename, book_filename):
    uris_to_book_infos = get_all_citations(readera_backup_filename)
    for book_info in uris_to_book_infos.values():
        if book_info.filename == book_filename:
            return book_info
    return None

def show_citations_for_book(readera_backup_filename, book):
    if Path(book).is_file():
        print(f"Searching for file '{book}'")
        book_info = get_citations_by_file(readera_backup_filename, book)
    else:
        print(f"Searching for title '{book}'")
        uris_to_book_infos = get_all_citations(readera_backup_filename)
        for book_info in uris_to_book_infos.values():
            if book.lower() in book_info.title.lower():
                break
        else:
            book_info = None

    if not book_info:
        print(f"Book not found")
        sys.exit(1)

    print(f"Book: {book_info.title}")
    for citation in sorted(book_info.citations, key=lambda item: item[1]):
        print(f"- {citation[0]}")
        print()

def tokenize(text):
    return re.findall(r'''[A-Za-z0-9!?.,;:'"]''', text)

def find_in(text: str, span: str) -> tuple[int, int, str]:
    # Returns (start_index_of_match, end_index_of_match, remainder of span that was not matched)
    # If a remainder is returned, the match was not complete but ended at the end of the text.
    # When no prefix of span could be found, start_index_of_match and end_index_of_match are None.
    try:
        match_index = text.index(span)
        return match_index, match_index + len(span) - 1, None
    except ValueError:
        for i in range(1, len(span)):
            subspan = span[:-i]
            if text.endswith(subspan):
                return len(text) - len(subspan), len(text) - 1, span[len(subspan):]
    return None, None, span

def add_citations_to_pdf(pdf_filename, citations):
    doc = fitz.open(pdf_filename)
    pages = [(page, page.get_textpage(flags=fitz.TEXT_MEDIABOX_CLIP)) for page in doc.pages()]

    page_index = page_index_last_found = 0
    num_found = num_not_found = 0

    @cache
    def get_textpage_words(page_index) -> tuple[str, list[tuple[int, int, int, int]]]:
        words = []
        coords = []
        for word_info in pages[page_index][1].extractWORDS():
            word = word_info[4]
            word_tokenized = tokenize(word)
            words += word_tokenized
            coords += [word_info[:4]] * len(word_tokenized)
        return ''.join(words), coords

    for citation, _, note in sorted(citations, key=lambda item: item[1]):
        citation_parts = citation.split('\n')
        for citation_part in citation_parts:
            citation_string = ''.join(tokenize(citation_part))
            remainder = citation_string
            highlights = []
            while page_index < len(pages):
                page, _ = pages[page_index]
                textpage_string, char_coords = get_textpage_words(page_index)

                match_start, match_end, remainder = find_in(textpage_string, remainder)
                if match_start is None:
                    remainder = citation_string
                    highlights = []
                else:
                    if len(highlights) > 0 and match_start > 0:
                        # This is a partial match which had a partial match on the previous page. But it
                        # does not start at the beginning of the page, so it is not a true continuation.
                        # - Ignore match from previous page
                        # - Check current page for the full citation
                        remainder = citation_string
                        highlights = []
                        continue
                    else:
                        highlights.append((page, fitz.Point(char_coords[match_start][:2]),
                                           fitz.Point(char_coords[match_end][2:])))

                if remainder:
                    page_index += 1
                else:
                    for page, start_coords, stop_coords in highlights:
                        page.add_highlight_annot(start=start_coords, stop=stop_coords)
                        if note is not None:
                            page.add_text_annot(start_coords - fitz.Point(8, 16), note)
                    page_index_last_found = page_index
                    num_found += 1
                    break
            else:
                print(f"Citation not found: {citation_part=}")
                num_not_found += 1
                page_index = page_index_last_found

    doc.saveIncr()

    print(f"Citations found: {num_found}")
    if num_not_found > 0:
        print(f"Citations not found: {num_not_found}")
        if '--debug' in sys.argv:
            with open('book.txt', 'w') as f:
                for _, textpage in pages:
                    print(textpage.extractText().encode(), file=f)
                    print(file=f)
        return False
    return True

def book_to_pdf(book_filename, pdf_filename):
    # Only really tested with epub
    # Possibly add --embed-all-fonts --subset-embedded-font
    subprocess.check_call(['ebook-convert', book_filename, pdf_filename])

def make_highlighted_pdf(readera_backup_filename, book_filename):
    book_info = get_citations_by_file(readera_backup_filename, book_filename)

    if not book_info:
        print(f"Book {book_filename} not found")
        sys.exit(1)

    pdf_filename = Path(book_filename).with_suffix('.pdf')
    book_to_pdf(book_filename, pdf_filename)
    success = add_citations_to_pdf(pdf_filename, book_info.citations)
    print(f"Produced highlighted PDF file: {pdf_filename}")
    if success:
        print('OK')
    else:
        print('ERROR')
        sys.exit(1)

def help():
    print(f"Usage: {sys.argv[0]} <cmd> [args]")
    print("Commands:")
    print("  verify-citations-complete")
    print("    Checks all backup files in the current directory and shows which have all citations")
    print("  show-titles")
    print("    Show all books with citations")
    print("  show-citations <file or book title substring>")
    print("  make-highlighted-pdf <book_filename>")
    print("    Convert book to PDF and add highlights from ReadEra backup")


sys.stdout.reconfigure(encoding='utf-8')
try:
    cmd = sys.argv[1]
except IndexError:
    help()
    sys.exit()

if cmd == 'verify-citations-complete':
    verify_citations_complete()
else:
    readera_backup_filename = os.getenv('READERA_BACKUP')
    if readera_backup_filename is None:
        readera_backup_filenames = tuple(Path('.').glob('ReadEra*.bak'))
        if len(readera_backup_filenames) == 0:
            print('Did not find any ReadEra backup in current directory')
            sys.exit(1)
        else:
            readera_backup_filename = max(readera_backup_filenames, key=lambda f: f.stat().st_mtime)
    print(f"Using {readera_backup_filename}")
    if cmd == 'show-titles':
        show_titles(readera_backup_filename)
    elif cmd == 'show-citations':
        book_title = sys.argv[2]
        show_citations_for_book(readera_backup_filename, book_title)
    elif cmd == 'make-highlighted-pdf':
        book_filename = sys.argv[2]
        make_highlighted_pdf(readera_backup_filename, book_filename)
    else:
        help()
        sys.exit(1)
