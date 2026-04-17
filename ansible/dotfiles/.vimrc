set nocompatible
filetype plugin indent on

" Make backspace behave in a sane manner.
set backspace=indent,eol,start

" Use jk for esc
:inoremap jk <Esc>
:inoremap Jk <Esc>
:inoremap JK <Esc>
:inoremap jK <Esc>

" Use comma as leader
:let mapleader = ","
set showcmd

" confim instead of error when leaving unsaved file
set confirm
" Allow hidden buffers, don't limit to 1 file per window/split
set hidden

" remember more than 8 commands of history
set history=400

" Stop auto-commenting
autocmd FileType * setlocal formatoptions-=c formatoptions-=r formatoptions-=o

" Set tab width properly
function! TabWidth(width)
  " Expand tabs to spaces
  setlocal expandtab
  " The width of a tab character in spaces
  execute "set tabstop=".a:width
  " The with of an indent, in spaces
  execute "setlocal shiftwidth=".a:width
  " Confusing. Set same as expandtab in most cases
  execute "setlocal softtabstop=".a:width
endfunction

" function for switching to prose mode
function! Prose()
  call TabWidth(4)
  set wrap linebreak
endfunction

" function for switching to code mode
" Use tab_width spaces for an indent
function! Code(tab_width)

  call TabWidth(a:tab_width)

  " Switch syntax highlighting on
  syntax on

  " Show line numbers
  set number

  " make a column at 81, 101 chars wide.
  set colorcolumn=81,101

endfunction

"" FILETYPE-SPECIFIC STUFF
function! Filetypes()
  if &filetype == "python"
    call Code(4)
  elseif &filetype == "markdown"
    call Prose()
  elseif &filetype == "text"
    call Prose()
  elseif &filetype == "make"
    call Code(2)
    set noexpandtab
  else
    call Code(2)
  endif
endfunction

autocmd FileType * call Filetypes()

" Use case insensitive search by default
set ignorecase
" If the search string contains a capital, then use case sensitive search
set smartcase

" This just enables the built-in folding. still has the dash bug.
" using * instead of - works tho.
let g:markdown_folding = 1
