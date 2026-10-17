"""Hand-written labeled predictions for library queries. See campus.py for the format."""
E, N = True, False

PAIRS = {
    1: [
        (E, "select b.title, a.fullname from books b join bookauthors ba on ba.bookid = b.id join authors a on a.id = ba.authorid",
         "lowercased camelCase identifiers fold to the same names"),
        (N, 'SELECT b.title, a.fullName FROM books b JOIN "bookAuthors" ba ON ba.bookId = b.id JOIN authors a ON a.id = ba.authorId',
         "INVALID: quoted \"bookAuthors\" is case-sensitive; the table is bookauthors"),
        (N, "SELECT b.title, string_agg(a.fullName, ', ') FROM books b JOIN bookAuthors ba ON ba.bookId = b.id JOIN authors a ON a.id = ba.authorId GROUP BY b.id, b.title",
         "one aggregated row per book"),
    ],
    2: [
        (E, "SELECT b.title FROM books b WHERE (SELECT COUNT(*) FROM bookAuthors ba WHERE ba.bookId = b.id) > 1",
         "correlated count"),
        (E, "SELECT b.title FROM books b WHERE EXISTS (SELECT 1 FROM bookAuthors x JOIN bookAuthors y ON x.bookId = y.bookId AND x.authorId <> y.authorId WHERE x.bookId = b.id)",
         "two distinct authors exist"),
        (N, "SELECT b.title FROM books b JOIN bookAuthors ba ON ba.bookId = b.id GROUP BY b.title HAVING COUNT(*) > 1",
         "books sharing a title are merged"),
    ],
    3: [
        (E, "SELECT DISTINCT fullname FROM authors WHERE id IN (SELECT ba.authorid FROM bookauthors ba JOIN bookgenres bg ON bg.bookid = ba.bookid JOIN genres g ON g.id = bg.genreid WHERE g.name = 'Science Fiction')",
         "semi-join with DISTINCT"),
        (N, "SELECT fullName FROM authors WHERE id IN (SELECT ba.authorId FROM bookAuthors ba JOIN bookGenres bg ON bg.bookId = ba.bookId JOIN genres g ON g.id = bg.genreId WHERE g.name = 'Science Fiction')",
         "no DISTINCT: different authors with the same name are both listed"),
        (N, "SELECT DISTINCT a.fullName FROM authors a JOIN bookAuthors ba ON ba.authorId = a.id JOIN bookGenres bg ON bg.bookId = ba.bookId JOIN genres g ON g.id = bg.genreId WHERE g.name LIKE 'Science%'",
         "prefix match includes other genres"),
    ],
    4: [
        (E, "SELECT b.title, m.name FROM members m JOIN loans l ON l.memberId = m.id JOIN copies c ON c.id = l.copyId JOIN books b ON b.id = c.bookId WHERE l.returnedOn IS NULL",
         "join order"),
        (N, "SELECT b.title, m.name FROM loans l JOIN copies c ON c.id = l.copyId JOIN books b ON b.id = c.bookId JOIN members m ON m.id = l.memberId WHERE l.returnedOn IS NOT NULL",
         "returned loans instead of current"),
        (N, "SELECT b.title, p.name FROM loans l JOIN copies c ON c.id = l.copyId JOIN books b ON b.id = c.bookId JOIN publishers p ON p.id = b.publisherId WHERE l.returnedOn IS NULL",
         "generic column: publisher name instead of member name"),
    ],
    5: [
        (E, "SELECT id FROM loans WHERE returnedon IS NULL AND dueon < '2024-06-01'::date",
         "cast syntax"),
        (N, "SELECT id FROM loans WHERE dueOn <= DATE '2024-06-01' AND returnedOn IS NULL",
         "includes loans due on June 1"),
        (N, "SELECT id FROM loans WHERE dueOn < DATE '2024-06-01' AND (returnedOn IS NULL OR returnedOn > dueOn)",
         "also includes loans that were returned late"),
    ],
    6: [
        (E, "SELECT m.name, m.email FROM members m WHERE m.newsletter = 'on'",
         "alias"),
        (N, "SELECT name, email FROM members WHERE newsletter = 'on' AND email IS NOT NULL",
         "drops subscribers without an email"),
        (N, "SELECT name, email FROM members WHERE newsletter = 'ON'",
         "case-sensitive string comparison"),
    ],
    7: [
        (E, "SELECT s.title, (SELECT p.title FROM books p WHERE p.id = s.sequelOfId) FROM books s WHERE s.sequelOfId IS NOT NULL",
         "scalar subquery over the FK"),
        (N, "SELECT s.title, p.title FROM books s JOIN books p ON p.sequelOfId = s.id",
         "self-join direction reversed"),
    ],
    8: [
        (E, "SELECT b.title FROM books b WHERE b.sequelOfId IS NULL AND b.id IN (SELECT sequelOfId FROM books)",
         "IN ignores the NULL sequelOfId values"),
        (N, "SELECT DISTINCT b.title FROM books b JOIN books s ON s.sequelOfId = b.id WHERE b.sequelOfId IS NULL",
         "DISTINCT on title merges different books with the same title"),
        (N, "SELECT b.title FROM books b WHERE EXISTS (SELECT 1 FROM books s WHERE s.sequelOfId = b.id)",
         "includes books in the middle of a series"),
    ],
    9: [
        (E, "SELECT g.name, COUNT(*) FROM genres g JOIN bookGenres bg ON bg.genreId = g.id JOIN copies c ON c.bookId = bg.bookId JOIN loans l ON l.copyId = c.id GROUP BY g.name",
         "genre name is UNIQUE NOT NULL"),
        (N, "SELECT g.name, COUNT(*) FROM genres g LEFT JOIN bookGenres bg ON bg.genreId = g.id LEFT JOIN copies c ON c.bookId = bg.bookId LEFT JOIN loans l ON l.copyId = c.id GROUP BY g.id, g.name",
         "genres without loans appear with count 1"),
    ],
    10: [
        (E, """WITH RECURSIVE sub AS (
    SELECT id, name FROM genres WHERE parentGenreId = (SELECT id FROM genres WHERE name = 'Fiction')
    UNION ALL
    SELECT g.id, g.name FROM genres g JOIN sub ON g.parentGenreId = sub.id
)
SELECT name FROM sub""",
         "scalar anchor: genre name is UNIQUE"),
        (N, "SELECT g.name FROM genres g JOIN genres p ON p.id = g.parentGenreId WHERE p.name = 'Fiction'",
         "direct children only"),
        (N, """WITH RECURSIVE sub AS (
    SELECT id, name FROM genres WHERE name = 'Fiction'
    UNION ALL
    SELECT g.id, g.name FROM genres g JOIN sub ON g.parentGenreId = sub.id
)
SELECT name FROM sub""",
         "includes Fiction itself"),
    ],
    11: [
        (E, "SELECT a.fullName FROM authors a WHERE a.deathYear IS NULL AND (SELECT COUNT(*) FROM bookAuthors ba WHERE ba.authorId = a.id) > 3",
         "correlated count"),
        (N, "SELECT a.fullName FROM authors a JOIN bookAuthors ba ON ba.authorId = a.id WHERE a.deathYear IS NULL GROUP BY a.id, a.fullName HAVING COUNT(*) >= 3",
         "three or more"),
        (N, "SELECT a.fullName FROM authors a JOIN bookAuthors ba ON ba.authorId = a.id GROUP BY a.id, a.fullName HAVING COUNT(*) > 3",
         "includes deceased authors"),
    ],
    12: [
        (E, "SELECT p.name, AVG(b.pageCount) FROM books b JOIN publishers p ON b.publisherId = p.id GROUP BY p.id, p.name",
         "join order"),
        (N, "SELECT p.name, AVG(b.pageCount) FROM publishers p LEFT JOIN books b ON b.publisherId = p.id GROUP BY p.id, p.name",
         "publishers with no books appear with NULL"),
        (N, "SELECT p.name, SUM(b.pageCount) / COUNT(*) FROM publishers p JOIN books b ON b.publisherId = p.id GROUP BY p.id, p.name",
         "integer division, and books with NULL pageCount are counted"),
    ],
    13: [
        (E, """WITH c AS (SELECT cp.bookId, COUNT(*) AS n FROM loans l JOIN copies cp ON cp.id = l.copyId GROUP BY cp.bookId)
SELECT b.title, c.n FROM c JOIN books b ON b.id = c.bookId ORDER BY c.n DESC LIMIT 5""",
         "pre-aggregate by book id in a CTE"),
        (N, "SELECT b.title, COUNT(*) AS n FROM loans l JOIN copies c ON c.id = l.copyId JOIN books b ON b.id = c.bookId GROUP BY b.title ORDER BY n DESC LIMIT 5",
         "books sharing a title are merged"),
        (N, "SELECT b.title, COUNT(DISTINCT c.id) AS n FROM loans l JOIN copies c ON c.id = l.copyId JOIN books b ON b.id = c.bookId GROUP BY b.id, b.title ORDER BY n DESC LIMIT 5",
         "counts copies borrowed, not loans"),
    ],
    14: [
        (E, "SELECT name FROM members WHERE id NOT IN (SELECT memberId FROM loans)",
         "NOT IN is safe: loans.memberId is NOT NULL"),
        (N, "SELECT name FROM members WHERE id NOT IN (SELECT memberId FROM loans WHERE returnedOn IS NULL)",
         "members with no current loan"),
    ],
    15: [
        (E, "SELECT COUNT(*) - COUNT(isbn) FROM books",
         "rows minus non-NULL isbns"),
        (N, "SELECT COUNT(*) FROM books WHERE isbn = ''",
         "empty string is not NULL"),
        (N, "SELECT COUNT(isbn) FROM books WHERE isbn IS NULL",
         "COUNT of the NULL column is always 0"),
    ],
    16: [
        (E, "SELECT AVG(returnedOn - loanedOn) FROM loans",
         "AVG ignores NULL differences, and with no GROUP BY the filter changes nothing"),
        (N, "SELECT AVG(dueOn - loanedOn) FROM loans WHERE returnedOn IS NOT NULL",
         "loan period, not actual duration"),
        (N, "SELECT SUM(returnedOn - loanedOn) / COUNT(returnedOn) FROM loans",
         "integer division"),
    ],
    17: [
        (E, "SELECT branch, COUNT(id) FROM copies WHERE circulating = 'on' GROUP BY 1",
         "ordinal GROUP BY"),
        (N, "SELECT branch, COUNT(*) FROM copies GROUP BY branch",
         "counts non-circulating copies"),
        (N, "SELECT branch, COUNT(DISTINCT bookId) FROM copies WHERE circulating = 'on' GROUP BY branch",
         "distinct titles, not copies"),
    ],
    18: [
        (E, "SELECT DISTINCT name FROM members m WHERE autorenew = 'off' AND EXISTS (SELECT 1 FROM loans l WHERE l.memberid = m.id AND l.returnedon IS NULL)",
         "EXISTS with DISTINCT"),
        (N, "SELECT m.name FROM members m JOIN loans l ON l.memberId = m.id WHERE m.autoRenew = 'off' AND l.returnedOn IS NULL",
         "one row per open loan"),
        (N, "SELECT DISTINCT m.name FROM members m JOIN loans l ON l.memberId = m.id WHERE m.autoRenew = 'on' AND l.returnedOn IS NULL",
         "auto-renew on instead of off"),
    ],
    19: [
        (E, "SELECT a.fullName, b.title FROM authors a, bookAuthors ba, books b WHERE ba.authorId = a.id AND b.id = ba.bookId AND ba.authorOrder = 1 AND b.publishedYear > 2000",
         "implicit joins"),
        (N, "SELECT a.fullName, b.title FROM authors a JOIN bookAuthors ba ON ba.authorId = a.id JOIN books b ON b.id = ba.bookId WHERE ba.authorOrder = 1 AND b.publishedYear >= 2000",
         "includes books published in 2000"),
        (N, "SELECT a.fullName, b.title FROM authors a JOIN bookAuthors ba ON ba.authorId = a.id JOIN books b ON b.id = ba.bookId WHERE b.publishedYear > 2000",
         "any author position"),
    ],
    20: [
        (E, "SELECT language, COUNT(*) FROM books GROUP BY language ORDER BY 2 DESC",
         "ordinal ORDER BY"),
        (N, "SELECT language, COUNT(*) FROM books GROUP BY language",
         "target is ordered; prediction is not"),
        (N, "SELECT language, COUNT(*) FROM books GROUP BY language ORDER BY language",
         "ordered by language"),
    ],
}
