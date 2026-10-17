-- library: public lending library
-- Shape:    many-to-many (books <-> authors via bookAuthors, books <-> genres via bookGenres)
--           plus self-references (books.sequelOfId -> books, genres.parentGenreId -> genres)
-- Naming:   camelCase, UNquoted -- PostgreSQL folds these to lowercase, so bookAuthors and
--           bookauthors name the same table
-- Flags:    VARCHAR 'on'/'off' with CHECK constraints
-- Generic:  id / name / bookId appear in several tables

CREATE TABLE authors (
    id           INTEGER PRIMARY KEY,
    fullName     VARCHAR(200) NOT NULL,
    birthYear    INTEGER,                                        -- nullable
    deathYear    INTEGER,                                        -- null = living (or unknown)
    nationality  VARCHAR(50)                                     -- nullable
);

CREATE TABLE publishers (
    id       INTEGER PRIMARY KEY,
    name     VARCHAR(200) NOT NULL,
    country  VARCHAR(50)                                         -- nullable
);

CREATE TABLE books (
    id             INTEGER PRIMARY KEY,
    title          VARCHAR(300) NOT NULL,
    isbn           VARCHAR(13) UNIQUE,                           -- nullable
    publisherId    INTEGER REFERENCES publishers (id),           -- nullable
    publishedYear  INTEGER,                                      -- nullable
    pageCount      INTEGER,                                      -- nullable
    sequelOfId     INTEGER REFERENCES books (id),                -- null = not a sequel
    language       VARCHAR(20) NOT NULL DEFAULT 'English'
);

CREATE TABLE bookAuthors (
    bookId       INTEGER NOT NULL REFERENCES books (id),
    authorId     INTEGER NOT NULL REFERENCES authors (id),
    authorOrder  SMALLINT NOT NULL,
    PRIMARY KEY (bookId, authorId)
);

CREATE TABLE genres (
    id             INTEGER PRIMARY KEY,
    name           VARCHAR(100) NOT NULL UNIQUE,
    parentGenreId  INTEGER REFERENCES genres (id)                -- null = top-level genre
);

CREATE TABLE bookGenres (
    bookId   INTEGER NOT NULL REFERENCES books (id),
    genreId  INTEGER NOT NULL REFERENCES genres (id),
    PRIMARY KEY (bookId, genreId)
);

CREATE TABLE members (
    id          INTEGER PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    email       VARCHAR(255),                                    -- nullable
    joinedOn    DATE NOT NULL,
    newsletter  VARCHAR(3) NOT NULL DEFAULT 'off' CHECK (newsletter IN ('on', 'off')),
    autoRenew   VARCHAR(3) NOT NULL DEFAULT 'on'  CHECK (autoRenew IN ('on', 'off'))
);

CREATE TABLE copies (
    id           INTEGER PRIMARY KEY,
    bookId       INTEGER NOT NULL REFERENCES books (id),
    branch       VARCHAR(50) NOT NULL,
    acquiredOn   DATE,                                           -- nullable
    circulating  VARCHAR(3) NOT NULL DEFAULT 'on' CHECK (circulating IN ('on', 'off'))
);

CREATE TABLE loans (
    id          INTEGER PRIMARY KEY,
    copyId      INTEGER NOT NULL REFERENCES copies (id),
    memberId    INTEGER NOT NULL REFERENCES members (id),
    loanedOn    DATE NOT NULL,
    dueOn       DATE NOT NULL,
    returnedOn  DATE                                             -- null = still checked out
);
