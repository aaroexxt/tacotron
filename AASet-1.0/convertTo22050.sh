#!/bin/bash
clear;
echo "~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-";
echo "Wav-Converter: 44100 to 22050 khz";
echo "You need ffmpeg installed."
echo "~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-";
cwd=$(pwd);
in_dir="44100wavs/";
out_dir="wavs/";

echo "CWD: $cwd";
out_dir=${out_dir%/} #remove trailing slashes
in_dir=${in_dir%/}
cwd=${cwd%/}

array=(`ls $cwd/$in_dir/`) # find array of wav files
len=${#array[*]}

if [[ $len > 0 ]]; then
    sudo mkdir $out_dir;
    i=0
    while [ $i -lt $len ]; do
        echo "Wav $i: ${array[$i]}"
        ffmpeg -i "$cwd/$in_dir/${array[$i]}" -ar 22050 "$cwd/$out_dir/${array[$i]}"; #convert wav
        let i++
    done
    echo "Done converting wav files.";
else
    echo "No wav files found in directory :(. Is the directory setting correct?";
fi